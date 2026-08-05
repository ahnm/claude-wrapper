import { Router, Request, Response } from 'express';
import { asyncHandler } from '../middleware/error';
import { CLAUDE_EFFORTS, CLAUDE_PERMISSION_MODES } from '../../utils/permission-mode';
import { buildModelVariants } from '../../utils/model-spec';
import { logger } from '../../utils/logger';

const router = Router();

// Working Claude models
const BASE_MODEL_IDS = ['sonnet', 'opus', 'fable'];

const toModel = (id: string) => ({
  id,
  object: 'model',
  owned_by: 'anthropic',
  created: 1709164800
});

// Base models first, then the model:effort variants a model dropdown can use
// to drive effort on clients that cannot send a top-level effort field.
const CLAUDE_MODELS = [
  ...BASE_MODEL_IDS.map(toModel),
  ...buildModelVariants(BASE_MODEL_IDS).map(toModel)
];

router.get('/v1/models', asyncHandler(async (_req: Request, res: Response) => {
  logger.info('Returning available Claude models', { count: CLAUDE_MODELS.length });

  res.json({
    object: 'list',
    data: CLAUDE_MODELS
  });
}));

router.get('/v1/efforts', asyncHandler(async (_req: Request, res: Response) => {
  logger.info('Returning available Claude effort levels', { count: CLAUDE_EFFORTS.length });

  res.json({
    object: 'list',
    data: CLAUDE_EFFORTS
  });
}));

router.get('/v1/permission-modes', asyncHandler(async (_req: Request, res: Response) => {
  logger.info('Returning available Claude permission modes', { count: CLAUDE_PERMISSION_MODES.length });

  res.json({
    object: 'list',
    data: CLAUDE_PERMISSION_MODES
  });
}));

export default router;