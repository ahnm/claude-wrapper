/**
 * Tests for ClaudeResolver
 * Tests Claude CLI command resolution and execution using proper mocking
 */

// Create the mock BEFORE any imports
const mockExecAsync = jest.fn();
const mockSpawn = jest.fn();

// Mock child_process
jest.mock('child_process', () => ({
  exec: jest.fn(),
  spawn: (...args: any[]) => mockSpawn(...args)
}));

// Mock util with our specific mock function
jest.mock('util', () => ({
  promisify: jest.fn(() => mockExecAsync)
}));

import { EventEmitter } from 'events';
import { PassThrough } from 'stream';
import { ClaudeResolver } from '../../../src/core/claude-resolver';
import { ClaudeCliError, TimeoutError } from '../../../src/utils/errors';

/**
 * Build a stand-in for the ChildProcess returned by spawn().
 * Prompts are delivered over stdin, so the double must be writable.
 */
function createMockChild(options: {
  stdout?: string;
  stderr?: string;
  code?: number | null;
  signal?: string | null;
  error?: Error;
} = {}): any {
  const child: any = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.stdin = new PassThrough();

  setImmediate(() => {
    if (options.error) {
      child.emit('error', options.error);
      return;
    }

    if (options.stdout) {
      child.stdout.write(options.stdout);
    }
    if (options.stderr) {
      child.stderr.write(options.stderr);
    }

    child.stdout.end();
    child.stderr.end();

    setImmediate(() => child.emit('close', options.code ?? 0, options.signal ?? null));
  });

  return child;
}

// Mock logger
jest.mock('../../../src/utils/logger', () => ({
  logger: {
    debug: jest.fn(),
    info: jest.fn(),
    warn: jest.fn(),
    error: jest.fn()
  }
}));

// Mock EnvironmentManager
jest.mock('../../../src/config/env', () => ({
  EnvironmentManager: {
    getConfig: jest.fn(() => ({
      port: 3000,
      timeout: 30000,
      claudeCommand: undefined,
      logLevel: 'info'
    }))
  }
}));

describe('ClaudeResolver', () => {
  let mockEnvironmentManager: any;

  beforeEach(() => {
    jest.clearAllMocks();
    
    // Reset EnvironmentManager mock to default
    mockEnvironmentManager = require('../../../src/config/env').EnvironmentManager;
    mockEnvironmentManager.getConfig.mockReturnValue({
      port: 3000,
      timeout: 30000,
      claudeCommand: undefined,
      logLevel: 'info'
    });
  });

  describe('findClaudeCommand', () => {
    describe('configuration-based resolution', () => {
      it('should use claude command from config when available', async () => {
        mockEnvironmentManager.getConfig.mockReturnValue({
          port: 3000,
          claudeCommand: '/config/claude',
          timeout: 30000,
          logLevel: 'info'
        });
        
        const resolver = new ClaudeResolver();
        const command = await resolver.findClaudeCommand();
        
        expect(command).toBe('/config/claude');
      });
    });

    describe('PATH resolution', () => {
      it('should find Claude via bash interactive shell', async () => {
        mockExecAsync
          .mockResolvedValueOnce({ stdout: '/usr/local/bin/claude', stderr: '' })
          .mockResolvedValueOnce({ stdout: 'Claude CLI v1.0.0 @anthropic-ai', stderr: '' });

        const resolver = new ClaudeResolver();
        const command = await resolver.findClaudeCommand();
        
        expect(command).toBe('/usr/local/bin/claude');
      });

      it('should handle Docker container detection', async () => {
        // Mock all PATH resolution attempts to fail, then Docker to succeed
        mockExecAsync
          .mockRejectedValueOnce(new Error('Command not found'))  // bash -i -c "which claude"
          .mockRejectedValueOnce(new Error('Command not found'))  // zsh -i -c "which claude"
          .mockRejectedValueOnce(new Error('Command not found'))  // command -v claude
          .mockRejectedValueOnce(new Error('Command not found'))  // which claude
          .mockResolvedValueOnce({ stdout: 'Claude CLI v1.0.0 @anthropic-ai', stderr: '' })  // docker run --rm anthropic/claude --version
          .mockResolvedValueOnce({ stdout: 'Claude CLI v1.0.0 @anthropic-ai', stderr: '' });  // validation call

        const resolver = new ClaudeResolver();
        const command = await resolver.findClaudeCommand();
        
        expect(command).toBe('docker run --rm anthropic/claude');
      });
    });

    describe('error handling', () => {
      it('should throw ClaudeCliError when no command found', async () => {
        mockExecAsync.mockRejectedValue(new Error('Command not found'));

        const resolver = new ClaudeResolver();
        await expect(resolver.findClaudeCommand()).rejects.toThrow(ClaudeCliError);
      });
    });
  });

  describe('executeClaudeCommand', () => {

    describe('command construction', () => {
      it('should construct regular command correctly', async () => {
        // Setup resolver with found command first
        mockExecAsync
          .mockResolvedValueOnce({ stdout: '/usr/local/bin/claude', stderr: '' })
          .mockResolvedValueOnce({ stdout: 'Claude CLI v1.0.0', stderr: '' });
        mockSpawn.mockReturnValueOnce(createMockChild({ stdout: 'Claude response' }));

        const resolver = new ClaudeResolver();
        await resolver.findClaudeCommand(); // Cache the command
        const result = await resolver.executeClaudeCommand('test prompt', 'sonnet');

        expect(result).toBe('Claude response');

        // Binary and flags are passed as argv, not through a shell
        const [command, args, options] = mockSpawn.mock.calls[0];
        expect(command).toBe('/usr/local/bin/claude');
        expect(args).toEqual(['--print', '--model', 'sonnet']);
        expect(options.shell).toBe(false);
        expect(options.windowsHide).toBe(true);
      });

      it('should pass effort and permission mode flags when provided', async () => {
        mockExecAsync
          .mockResolvedValueOnce({ stdout: '/usr/local/bin/claude', stderr: '' })
          .mockResolvedValueOnce({ stdout: 'Claude CLI v1.0.0', stderr: '' });
        mockSpawn.mockReturnValueOnce(createMockChild({ stdout: 'Claude response' }));

        const resolver = new ClaudeResolver();
        await resolver.findClaudeCommand();
        await resolver.executeClaudeCommandWithSession(
          'test prompt',
          'sonnet',
          'session-123',
          false,
          'high',
          'acceptEdits'
        );

        expect(mockSpawn.mock.calls[0][1]).toEqual([
          '--print', '--model', 'sonnet',
          '--resume', 'session-123',
          '--effort', 'high',
          '--permission-mode', 'acceptEdits'
        ]);
      });

      it('should normalize fable model aliases', async () => {
        mockExecAsync
          .mockResolvedValueOnce({ stdout: '/usr/local/bin/claude', stderr: '' })
          .mockResolvedValueOnce({ stdout: 'Claude CLI v1.0.0', stderr: '' });
        mockSpawn.mockReturnValueOnce(createMockChild({ stdout: 'Claude response' }));

        const resolver = new ClaudeResolver();
        await resolver.findClaudeCommand();
        await resolver.executeClaudeCommand('test prompt', 'claude-fable-5');

        expect(mockSpawn.mock.calls[0][1]).toEqual(['--print', '--model', 'fable']);
      });

      it('should write the prompt to stdin rather than escaping it into a shell command', async () => {
        mockExecAsync
          .mockResolvedValueOnce({ stdout: '/usr/local/bin/claude', stderr: '' })
          .mockResolvedValueOnce({ stdout: 'Claude CLI v1.0.0', stderr: '' });

        const child = createMockChild({ stdout: 'Claude response' });
        mockSpawn.mockReturnValueOnce(child);

        const prompt = `it's a "quoted" prompt; rm -rf /`;
        const resolver = new ClaudeResolver();
        await resolver.findClaudeCommand();
        await resolver.executeClaudeCommand(prompt, 'sonnet');

        expect(child.stdin.read().toString()).toBe(prompt);
      });
    });

    describe('error handling', () => {
      it('should throw TimeoutError for timeout errors', async () => {
        // Setup resolver with found command first
        mockExecAsync
          .mockResolvedValueOnce({ stdout: '/usr/local/bin/claude', stderr: '' })
          .mockResolvedValueOnce({ stdout: 'Claude CLI v1.0.0', stderr: '' });
        mockSpawn.mockReturnValueOnce(createMockChild({ error: new Error('timeout exceeded') }));

        const resolver = new ClaudeResolver();
        await resolver.findClaudeCommand(); // Cache the command
        await expect(resolver.executeClaudeCommand('test prompt', 'sonnet'))
          .rejects.toThrow(TimeoutError);
      });

      it('should throw ClaudeCliError for other errors', async () => {
        // Setup resolver with found command first
        mockExecAsync
          .mockResolvedValueOnce({ stdout: '/usr/local/bin/claude', stderr: '' })
          .mockResolvedValueOnce({ stdout: 'Claude CLI v1.0.0', stderr: '' });
        mockSpawn.mockReturnValueOnce(createMockChild({ error: new Error('Permission denied') }));

        const resolver = new ClaudeResolver();
        await resolver.findClaudeCommand(); // Cache the command
        await expect(resolver.executeClaudeCommand('test prompt', 'sonnet'))
          .rejects.toThrow(ClaudeCliError);
      });

      it('should throw ClaudeCliError on a non-zero exit code', async () => {
        mockExecAsync
          .mockResolvedValueOnce({ stdout: '/usr/local/bin/claude', stderr: '' })
          .mockResolvedValueOnce({ stdout: 'Claude CLI v1.0.0', stderr: '' });
        mockSpawn.mockReturnValueOnce(createMockChild({ stderr: 'boom', code: 1 }));

        const resolver = new ClaudeResolver();
        await resolver.findClaudeCommand();
        await expect(resolver.executeClaudeCommand('test prompt', 'sonnet'))
          .rejects.toThrow(ClaudeCliError);
      });
    });
  });
});