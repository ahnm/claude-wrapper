import { exec } from 'child_process';
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
        const { stdout } = await execAsync(pathCmd, { timeout: 2000 });
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

  async executeClaudeCommand(prompt: string, model: string, signal?: AbortSignal): Promise<string> {
    return this.executeClaudeCommandWithSession(prompt, model, null, false, signal);
  }

  async executeClaudeCommandWithSession(
    prompt: string,
    model: string,
    sessionId: string | null,
    useJsonOutput: boolean,
    signal?: AbortSignal
  ): Promise<string> {
    const claudeCmd = await this.findClaudeCommand();
    const config = EnvironmentManager.getConfig();
    
    // Build command flags
    let flags = `--print --model ${model}`;
    
    // Add session flag if provided
    if (sessionId) {
      flags += ` --resume ${sessionId}`;
    }
    
    // Add JSON output flag if requested
    if (useJsonOutput) {
      flags += ` --output-format json`;
    }
    
    // The prompt is piped via stdin (see below) instead of being embedded in
    // the command line with echo: large prompts exceed Windows' ~32K
    // CreateProcess command-line limit (spawn ENAMETOOLONG).
    let command: string;

    // Handle Docker commands
    if (claudeCmd.includes('docker run') || claudeCmd.includes('podman run')) {
      command = `${claudeCmd} ${flags}`;
    }
    // Handle bash -c wrapped commands
    else if (claudeCmd.includes('bash -c')) {
      command = claudeCmd.replace('"claude"', `"claude ${flags}"`);
    }
    // Handle regular commands
    else {
      command = `${claudeCmd} ${flags}`;
    }

    logger.debug('Executing Claude command with session', { 
      model, 
      promptLength: prompt.length, 
      sessionId,
      useJsonOutput,
      isDocker: claudeCmd.includes('docker') || claudeCmd.includes('podman')
    });
    
    try {
      // signal: aborting kills the CLI subprocess (client disconnected)
      const childPromise = execAsync(command, {
        maxBuffer: 1024 * 1024 * 10,
        timeout: config.timeout,
        ...(signal ? { signal } : {})
      });
      childPromise.child.stdin?.write(prompt);
      childPromise.child.stdin?.end();
      const { stdout, stderr } = await childPromise;

      if (stderr && stderr.trim()) {
        logger.warn('Claude CLI warning', { stderr: stderr.trim() });
      }
      
      logger.debug('Claude command completed successfully');
      return stdout.trim();
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';

      if (signal?.aborted) {
        logger.warn('Claude CLI execution aborted (client disconnected)');
        throw new ClaudeCliError('Claude CLI execution aborted by client disconnect');
      }

      logger.error('Claude CLI execution failed', error as Error);

      if (errorMessage.includes('timeout')) {
        throw new TimeoutError(`Claude CLI execution timed out after ${config.timeout}ms`);
      }

      throw new ClaudeCliError(`Claude CLI execution failed: ${errorMessage}`);
    }
  }

  private async testClaudeCommand(command: string): Promise<boolean> {
    try {
      const testCmd = `${command} --version`;
      const { stdout, stderr } = await execAsync(testCmd, { timeout: 3000 });
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