import { Router, Request, Response } from 'express';
import { asyncHandler } from '../middleware/error';
import { CLAUDE_EFFORTS, CLAUDE_PERMISSION_MODES } from '../../utils/permission-mode';
import { logger } from '../../utils/logger';

const router = Router();

// Working Claude models
const CLAUDE_MODELS = [
  { id: 'sonnet', object: 'model', owned_by: 'anthropic', created: 1709164800 },
  { id: 'opus', object: 'model', owned_by: 'anthropic', created: 1709164800 },
  { id: 'fable', object: 'model', owned_by: 'anthropic', created: 1709164800 }
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