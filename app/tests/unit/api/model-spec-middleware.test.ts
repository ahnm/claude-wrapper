/**
 * Tests for the model spec decoding middleware
 */

import { Request, Response, NextFunction } from 'express';
import { modelSpecMiddleware } from '../../../src/api/middleware/model-spec';
import '../../mocks/logger.mock';

describe('modelSpecMiddleware', () => {
  let req: Partial<Request>;
  let res: Partial<Response>;
  let next: NextFunction;

  beforeEach(() => {
    req = { body: {} };
    res = {};
    next = jest.fn();
  });

  test('should decode effort and permission mode out of the model name', () => {
    req.body = { model: 'opus:high:plan', messages: [] };

    modelSpecMiddleware(req as Request, res as Response, next);

    expect(req.body.model).toBe('opus');
    expect(req.body.effort).toBe('high');
    expect(req.body.permission_mode).toBe('plan');
    expect(next).toHaveBeenCalledWith();
  });

  test('should leave a plain model name untouched', () => {
    req.body = { model: 'opus', messages: [] };

    modelSpecMiddleware(req as Request, res as Response, next);

    expect(req.body.model).toBe('opus');
    expect(req.body).not.toHaveProperty('effort');
    expect(req.body).not.toHaveProperty('permission_mode');
    expect(next).toHaveBeenCalledWith();
  });

  test('should let an explicit top-level effort win over the model segment', () => {
    req.body = { model: 'opus:high', effort: 'low', messages: [] };

    modelSpecMiddleware(req as Request, res as Response, next);

    expect(req.body.model).toBe('opus');
    expect(req.body.effort).toBe('low');
  });

  test('should let an explicit permission mode win in any spelling', () => {
    req.body = { model: 'opus:plan', permissionMode: 'manual', messages: [] };

    modelSpecMiddleware(req as Request, res as Response, next);

    expect(req.body.permissionMode).toBe('manual');
    expect(req.body).not.toHaveProperty('permission_mode');
  });

  test('should pass through when there is no body', () => {
    req.body = undefined;

    modelSpecMiddleware(req as Request, res as Response, next);

    expect(next).toHaveBeenCalledWith();
  });

  test('should pass through when model is not a string', () => {
    req.body = { model: 42, messages: [] };

    modelSpecMiddleware(req as Request, res as Response, next);

    expect(req.body.model).toBe(42);
    expect(next).toHaveBeenCalledWith();
  });

  test('should not strip an unrecognized segment from an unencoded model', () => {
    req.body = { model: 'claude-fable-5', messages: [] };

    modelSpecMiddleware(req as Request, res as Response, next);

    expect(req.body.model).toBe('claude-fable-5');
  });
});
