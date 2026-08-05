/**
 * Tests for permission mode normalization
 */

import {
  normalizePermissionMode,
  CLAUDE_EFFORTS,
  CLAUDE_PERMISSION_MODES
} from '../../../src/utils/permission-mode';

describe('normalizePermissionMode', () => {
  test('should return undefined for missing source', () => {
    expect(normalizePermissionMode(undefined)).toBeUndefined();
  });

  test('should return undefined when no spelling is present', () => {
    expect(normalizePermissionMode({})).toBeUndefined();
  });

  test('should read permission_mode', () => {
    expect(normalizePermissionMode({ permission_mode: 'auto' })).toBe('auto');
  });

  test('should read permissionMode', () => {
    expect(normalizePermissionMode({ permissionMode: 'plan' })).toBe('plan');
  });

  test('should read permission-mode', () => {
    expect(normalizePermissionMode({ 'permission-mode': 'manual' })).toBe('manual');
  });

  test('should prefer permission_mode over the other spellings', () => {
    const resolved = normalizePermissionMode({
      permission_mode: 'auto',
      permissionMode: 'plan',
      'permission-mode': 'manual'
    });

    expect(resolved).toBe('auto');
  });

  test('should prefer permissionMode over permission-mode', () => {
    expect(normalizePermissionMode({ permissionMode: 'plan', 'permission-mode': 'manual' })).toBe('plan');
  });
});

describe('Claude CLI option lists', () => {
  test('should expose the supported effort levels', () => {
    expect(CLAUDE_EFFORTS).toEqual(['low', 'medium', 'high', 'xhigh', 'max']);
  });

  test('should expose the supported permission modes', () => {
    expect(CLAUDE_PERMISSION_MODES).toEqual([
      'acceptEdits',
      'auto',
      'bypassPermissions',
      'manual',
      'dontAsk',
      'plan'
    ]);
  });
});
