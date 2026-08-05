/**
 * Tests for model spec parsing
 */

import { parseModelSpec, buildModelVariants } from '../../../src/utils/model-spec';
import '../../mocks/logger.mock';

describe('parseModelSpec', () => {
  test('should pass a plain model name through unchanged', () => {
    const spec = parseModelSpec('opus');

    expect(spec.model).toBe('opus');
    expect(spec.effort).toBeUndefined();
    expect(spec.permissionMode).toBeUndefined();
  });

  test('should split model and effort', () => {
    const spec = parseModelSpec('opus:high');

    expect(spec.model).toBe('opus');
    expect(spec.effort).toBe('high');
    expect(spec.permissionMode).toBeUndefined();
  });

  test('should split model, effort and permission mode', () => {
    const spec = parseModelSpec('opus:high:plan');

    expect(spec.model).toBe('opus');
    expect(spec.effort).toBe('high');
    expect(spec.permissionMode).toBe('plan');
  });

  test('should classify segments by membership, not position', () => {
    const spec = parseModelSpec('opus:plan:high');

    expect(spec.model).toBe('opus');
    expect(spec.effort).toBe('high');
    expect(spec.permissionMode).toBe('plan');
  });

  test('should accept a permission mode without an effort', () => {
    const spec = parseModelSpec('sonnet:acceptEdits');

    expect(spec.model).toBe('sonnet');
    expect(spec.effort).toBeUndefined();
    expect(spec.permissionMode).toBe('acceptEdits');
  });

  test('should restore canonical casing', () => {
    const spec = parseModelSpec('opus:HIGH:acceptedits');

    expect(spec.effort).toBe('high');
    expect(spec.permissionMode).toBe('acceptEdits');
  });

  test('should collect unrecognized segments instead of treating them as options', () => {
    const spec = parseModelSpec('opus:bogus');

    expect(spec.model).toBe('opus');
    expect(spec.effort).toBeUndefined();
    expect(spec.permissionMode).toBeUndefined();
    expect(spec.unknownSegments).toEqual(['bogus']);
  });

  test('should ignore empty segments', () => {
    const spec = parseModelSpec('opus::high');

    expect(spec.model).toBe('opus');
    expect(spec.effort).toBe('high');
    expect(spec.unknownSegments).toEqual([]);
  });

  test('should keep the first of a duplicated category', () => {
    const spec = parseModelSpec('opus:high:low');

    expect(spec.effort).toBe('high');
    expect(spec.unknownSegments).toEqual(['low']);
  });

  test('should handle undefined and empty input', () => {
    expect(parseModelSpec(undefined).model).toBe('');
    expect(parseModelSpec('').model).toBe('');
  });

  test('should not misread an alias that contains no separator', () => {
    expect(parseModelSpec('claude-fable-5').model).toBe('claude-fable-5');
  });
});

describe('buildModelVariants', () => {
  test('should produce a model:effort variant per effort level', () => {
    expect(buildModelVariants(['opus'])).toEqual([
      'opus:low',
      'opus:medium',
      'opus:high',
      'opus:xhigh',
      'opus:max'
    ]);
  });

  test('should cover every base model', () => {
    const variants = buildModelVariants(['sonnet', 'opus']);

    expect(variants).toHaveLength(10);
    expect(variants).toContain('sonnet:max');
    expect(variants).toContain('opus:low');
  });

  test('should round-trip through parseModelSpec', () => {
    for (const variant of buildModelVariants(['sonnet', 'opus', 'fable'])) {
      const spec = parseModelSpec(variant);

      expect(['sonnet', 'opus', 'fable']).toContain(spec.model);
      expect(spec.effort).toBeDefined();
      expect(spec.unknownSegments).toEqual([]);
    }
  });
});
