/**
 * Model spec decoding middleware
 *
 * Rewrites a colon-encoded model name (`opus:high:plan`) into the bare model
 * plus the equivalent top-level `effort` / `permission_mode` fields, so the
 * rest of the pipeline only ever sees the decoded form.
 *
 * Precedence, highest first:
 *   1. an explicit top-level field on the request
 *   2. a segment encoded in the model name
 *   3. whatever the session already remembers (applied later, in session.ts)
 *
 * An explicit field wins because it is the documented first-class API; the
 * model-name encoding exists for clients that cannot send one.
 */

import { Request, Response, NextFunction } from 'express';
import { parseModelSpec } from '../../utils/model-spec';
import { normalizePermissionMode } from '../../utils/permission-mode';
import { logger } from '../../utils/logger';

export function modelSpecMiddleware(req: Request, _res: Response, next: NextFunction): void {
  try {
    const body = req.body;

    if (!body || typeof body.model !== 'string') {
      next();
      return;
    }

    const spec = parseModelSpec(body.model);

    // Nothing encoded - leave the request untouched
    if (spec.effort === undefined && spec.permissionMode === undefined) {
      next();
      return;
    }

    const rawModel = body.model;
    body.model = spec.model;

    if (spec.effort !== undefined && body.effort === undefined) {
      body.effort = spec.effort;
    }

    if (spec.permissionMode !== undefined && normalizePermissionMode(body) === undefined) {
      body.permission_mode = spec.permissionMode;
    }

    logger.debug('Decoded model spec', {
      rawModel,
      model: body.model,
      effort: body.effort,
      permission_mode: body.permission_mode
    });

    next();
  } catch (error) {
    logger.warn('Model spec decoding failed; passing request through unchanged', {
      error: error instanceof Error ? error.message : String(error)
    });
    next();
  }
}
