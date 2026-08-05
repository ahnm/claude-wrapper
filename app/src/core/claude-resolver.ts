import { exec, spawn, SpawnOptions } from 'child_process';
import { promisify } from 'util';
import { IClaudeResolver } from '../types';
import { ClaudeCliError, TimeoutError } from '../utils/errors';
import { logger } from '../utils/logger';
import { EnvironmentManager } from '../config/env';

const execAsync = promisify(exec);

export class ClaudeResolver implements IClaudeResolver {
  private claudeCommand: string | null = null;

  async findClaudeCommand(): Promise<string> {
    if (this.claudeCommand) {
      return this.claudeCommand;
    }

    const config = EnvironmentManager.getConfig();
    if (config.claudeCommand) {
      logger.debug('Using Claude command from config', { command: config.claudeCommand });
      this.claudeCommand = config.claudeCommand;
      return config.claudeCommand;
    }

    // Try PATH resolution - covers npm global installs, aliases, and Docker
    const pathCommands = [
      // Interactive shells (handles aliases)
      'bash -i -c "which claude"',
      'zsh -i -c "which claude"',
      
      // Direct PATH lookups (handles npm global installs)
      'command -v claude',
      'which claude',
      
      // Docker detection (check if Docker containers are available)
      'docker run --rm anthropic/claude --version',
      'podman run --rm anthropic/claude --version'
    ];
    
    // Windows-specific commands
    if (process.platform === 'win32') {
      pathCommands.push(
        'where claude',
        'powershell -c "Get-Command claude -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source"'
      );
    }

    for (const pathCmd of pathCommands) {
      try {
        logger.debug('Trying PATH resolution', { command: pathCmd });
        const { stdout } = await execAsync(pathCmd, { timeout: 2000, windowsHide: true });
        const claudePath = stdout.trim();
        
        if (claudePath && !claudePath.includes('not found')) {
          // Clean up shell prompt output that might be mixed in
          const cleanedPath = claudePath.replace(/\]633;[^;]*;[^;]*;[^;]*;[^;]*]/g, '').trim();
          
          // Handle different command types
          let actualPath = cleanedPath;
          logger.debug('Processing Claude path detection', { claudePath: cleanedPath, pathCmd });
          
          // Handle Docker commands
          if (pathCmd.includes('docker run') || pathCmd.includes('podman run')) {
            // For Docker, the full command is the "path"
            actualPath = pathCmd.replace(' --version', '');
            logger.debug('Parsed Docker command', { actualPath });
          }
          // Handle shell alias output
          else if (cleanedPath.includes(': aliased to ')) {
            const splitPath = cleanedPath.split(': aliased to ')[1];
            actualPath = splitPath ? splitPath.trim() : cleanedPath;
            logger.debug('Parsed alias', { actualPath });
          } else if (cleanedPath.includes('aliased to ')) {
            const splitPath = cleanedPath.split('aliased to ')[1];
            actualPath = splitPath ? splitPath.trim() : cleanedPath;
            logger.debug('Parsed alias', { actualPath });
          } else {
            logger.debug('Using path as-is', { actualPath });
          }
          // Verify it works
          const testResult = await this.testClaudeCommand(actualPath);
          if (testResult) {
            logger.info('Found Claude via PATH resolution', { path: actualPath, original: claudePath });
            this.claudeCommand = actualPath;
            return actualPath;
          }
        }
      } catch (error) {
        logger.debug('PATH resolution failed', { command: pathCmd, error });
        continue;
      }
    }


    // Try environment variables as fallback
    const envVars = [
      process.env['CLAUDE_COMMAND'],
      process.env['CLAUDE_CLI_PATH'],
      process.env['CLAUDE_DOCKER_IMAGE'] ? `docker run --rm ${process.env['CLAUDE_DOCKER_IMAGE']}` : undefined,
      process.env['DOCKER_CLAUDE_CMD']
    ].filter(Boolean) as string[];

    for (const envPath of envVars) {
      try {
        logger.debug('Trying environment variable path', { path: envPath });
        const isWorking = await this.testClaudeCommand(envPath);
        
        if (isWorking) {
          logger.info('Found Claude via environment variable', { path: envPath });
          this.claudeCommand = envPath;
          return envPath;
        }
      } catch (error) {
        logger.debug('Environment path failed', { 
          path: envPath, 
          error: error instanceof Error ? error.message : 'Unknown error' 
        });
        continue;
      }
    }


    // No more guessing - fail clearly if not found
    throw new ClaudeCliError(
      'Claude CLI not found. Please either:\n' +
      '1. Install Claude CLI: npm install -g @anthropic-ai/claude\n' +
      '2. Use Docker: docker pull anthropic/claude\n' +
      '3. Ensure \'claude\' is in your PATH\n' +
      '4. Set CLAUDE_COMMAND environment variable with the correct path\n' +
      '\nSupported detection methods:\n' +
      '- npm global installs (recommended)\n' +
      '- Docker containers (docker run anthropic/claude)\n' +
      '- Shell aliases (bash, zsh)\n' +
      '- Environment variables (CLAUDE_COMMAND, CLAUDE_CLI_PATH, CLAUDE_DOCKER_IMAGE, etc.)'
    );
  }

  async executeClaudeCommand(prompt: string, model: string): Promise<string> {
    return this.executeClaudeCommandWithSession(prompt, model, null, false);
  }

  async executeClaudeCommandWithSession(
    prompt: string,
    model: string,
    sessionId: string | null,
    useJsonOutput: boolean,
    effort?: string,
    permissionMode?: string
  ): Promise<string> {
    const claudeCmd = await this.findClaudeCommand();
    const config = EnvironmentManager.getConfig();

    // Build command flags
    const normalizedModel = this.normalizeModel(model);
    let flags = `--print --model ${normalizedModel}`;

    // Add session flag if provided
    if (sessionId) {
      flags += ` --resume ${sessionId}`;
    }

    // Add effort flag if provided
    if (effort) {
      flags += ` --effort ${effort}`;
    }

    // Add permission mode flag if provided
    if (permissionMode) {
      flags += ` --permission-mode ${permissionMode}`;
    }

    // Add JSON output flag if requested
    if (useJsonOutput) {
      flags += ` --output-format json`;
    }

    // The prompt is written to stdin rather than interpolated into a shell
    // command, so it never needs escaping and cannot break the invocation.
    let useShell = false;
    let command: string;
    let args: string[] = [];

    // Handle Docker commands
    if (claudeCmd.includes('docker run') || claudeCmd.includes('podman run')) {
      // For Docker, we need to modify the container command
      command = `${claudeCmd} ${flags}`;
      useShell = true;
    }
    // Handle bash -c wrapped commands
    else if (claudeCmd.includes('bash -c')) {
      command = claudeCmd.replace('"claude"', `"claude ${flags}"`);
      useShell = true;
    }
    // Handle regular commands
    else {
      command = claudeCmd;
      args = flags.split(' ');
      useShell = false;
    }

    logger.debug('Executing Claude command with session', {
      model,
      promptLength: prompt.length,
      sessionId,
      useJsonOutput,
      isDocker: claudeCmd.includes('docker') || claudeCmd.includes('podman'),
      useShell,
      command,
      args
    });

    try {
      const { stdout, stderr } = await this.execWithInput(command, args, prompt, {
        maxBuffer: 1024 * 1024 * 10,
        timeout: config.timeout,
        shell: useShell,
        windowsHide: true
      });

      if (stderr && stderr.trim()) {
        logger.warn('Claude CLI warning', { stderr: stderr.trim() });
      }
      
      logger.debug('Claude command completed successfully');
      return stdout.trim();
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';
      logger.error('Claude CLI execution failed', error as Error);
      
      if (errorMessage.includes('timeout')) {
        throw new TimeoutError(`Claude CLI execution timed out after ${config.timeout}ms`);
      }
      
      throw new ClaudeCliError(`Claude CLI execution failed: ${errorMessage}`);
    }
  }

  /**
   * Spawn the Claude CLI and feed the prompt over stdin.
   * Avoids `echo '<prompt>' | claude`, which breaks on Windows (no POSIX shell)
   * and requires escaping arbitrary user content into a shell command line.
   */
  private async execWithInput(
    command: string,
    args: string[],
    input: string,
    options: {
      maxBuffer?: number;
      timeout?: number;
      shell?: boolean;
      windowsHide?: boolean;
    }
  ): Promise<{ stdout: string; stderr: string }> {
    return new Promise((resolve, reject) => {
      const spawnOptions: SpawnOptions = {
        stdio: ['pipe', 'pipe', 'pipe'],
        shell: options.shell || false,
        ...(options.timeout !== undefined && { timeout: options.timeout }),
        windowsHide: options.windowsHide === true
      };

      const child = args.length > 0 && !spawnOptions.shell
        ? spawn(command, args, spawnOptions)
        : spawn(command, spawnOptions);

      let stdout = '';
      let stderr = '';

      child.stdout?.on('data', (chunk: Buffer) => { stdout += chunk.toString(); });
      child.stderr?.on('data', (chunk: Buffer) => { stderr += chunk.toString(); });

      child.on('error', (error: Error) => reject(error));

      child.on('close', (code, signal) => {
        if (code === 0) {
          resolve({ stdout, stderr });
          return;
        }

        const exitMessage = code === null
          ? `Command exited due to signal ${signal}`
          : `Command exited with code ${code}${signal ? ` signal ${signal}` : ''}`;

        reject(new Error(`${exitMessage}\n${stderr}`));
      });

      if (child.stdin && child.stdin.writable) {
        child.stdin.write(input);
        child.stdin.end();
      }
    });
  }

  /**
   * Map OpenAI-style model aliases onto the names the Claude CLI accepts.
   */
  private normalizeModel(model: string): string {
    const normalized = String(model).toLowerCase();

    if (normalized === 'fable-5' || normalized === 'claude-fable-5') {
      return 'fable';
    }

    return model;
  }

  private async testClaudeCommand(command: string): Promise<boolean> {
    try {
      const testCmd = `${command} --version`;
      const { stdout, stderr } = await execAsync(testCmd, { timeout: 3000, windowsHide: true });
      const output = (stdout + stderr).toLowerCase();
      
      // Check for Claude CLI indicators
      return output.includes('claude') || 
             output.includes('anthropic') ||
             output.includes('@anthropic-ai');
    } catch (error) {
      logger.debug('Command test failed', { 
        command, 
        error: error instanceof Error ? error.message : 'Unknown error' 
      });
      return false;
    }
  }
}