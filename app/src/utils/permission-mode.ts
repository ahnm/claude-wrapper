/**
 * Permission mode normalization
 *
 * Clients spell the Claude CLI `--permission-mode` flag three different ways
 * depending on their conventions (snake_case, camelCase, kebab-case). All three
 * are accepted on the wire and collapsed to `permission_mode` internally.
 */

/** Effort levels accepted by the Claude CLI `--effort` flag. */
export const CLAUDE_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'] as const;

/** Permission modes accepted by the Claude CLI `--permission-mode` flag. */
export const CLAUDE_PERMISSION_MODES = [
  'acceptEdits',
  'auto',
  'bypassPermissions',
  'manual',
  'dontAsk',
  'plan'
] as const;

interface PermissionModeSource {
  permission_mode?: string | undefined;
  permissionMode?: string | undefined;
  'permission-mode'?: string | undefined;
}

/**
 * Resolve the permission mode from any of its accepted spellings.
 * Precedence: permission_mode > permissionMode > permission-mode.
 */
export function normalizePermissionMode(source: PermissionModeSource | undefined): string | undefined {
  if (!source) {
    return undefined;
  }

  if (source.permission_mode !== undefined) {
    return source.permission_mode;
  }

  if (source.permissionMode !== undefined) {
    return source.permissionMode;
  }

  return source['permission-mode'];
}
