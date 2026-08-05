import { Router, Request, Response } from 'express';
import { CoreWrapper } from '../../core/wrapper';
import { StreamingHandler } from '../../streaming/handler';
import { OpenAIRequest } from '../../types';
import { InvalidRequestError } from '../../utils/errors';
import { asyncHandler } from '../middleware/error';
import { streamingMiddleware } from '../middleware/streaming';
import { sessionProcessingMiddleware } from '../middleware/session';
import { modelSpecMiddleware } from '../middleware/model-spec';
import { normalizePermissionMode } from '../../utils/permission-mode';
import { logger } from '../../utils/logger';

const router = Router();
const coreWrapper = new CoreWrapper();
const streamingHandler = new StreamingHandler();

// Decode the model spec first so sessions store the decoded values
router.post('/v1/chat/completions',
  modelSpecMiddleware,
  sessionProcessingMiddleware,
  streamingMiddleware,
  asyncHandler(async (req: Request, res: Response) => {
    logger.info('Chat completion request received', {
      model: req.body.model,
      sessionId: req.body.session_id,
      effort: req.body.effort,
      permissionMode: normalizePermissionMode(req.body),
      messageCount: req.body.messages?.length,
      isStreaming: req.body.stream
    });

    const request: OpenAIRequest = req.body;

    // Collapse the accepted permission mode spellings to permission_mode
    const permissionMode = normalizePermissionMode(request);
    if (permissionMode !== undefined) {
      request.permission_mode = permissionMode;
    }

    // Validate required fields
    if (!request.messages || !Array.isArray(request.messages)) {
      throw new InvalidRequestError('Invalid request format: messages are required');
    }

    // A session remembers its model, so model is only required without one
    if (!request.model && !request.session_id) {
      throw new InvalidRequestError('Invalid request format: model is required when no session_id is provided');
    }

    if (request.messages.length === 0) {
      throw new InvalidRequestError('Messages array cannot be empty');
    }

    // Validate message format
    for (const message of request.messages) {
      if (!message.role || !['system', 'user', 'assistant', 'tool'].includes(message.role)) {
        throw new InvalidRequestError('Invalid message role. Must be one of: system, user, assistant, tool');
      }
      if (message.content === undefined || message.content === null) {
        throw new InvalidRequestError('Message content is required');
      }
    }

    // Handle streaming vs non-streaming requests
    if (request.stream) {
      logger.info('Processing streaming chat completion', {
        model: request.model,
        messageCount: request.messages.length
      });
      
      // Handle streaming request - response is managed by StreamingHandler
      await streamingHandler.handleStreamingRequest(request, res);
      
    } else {
      logger.info('Processing non-streaming chat completion', {
        model: request.model,
        messageCount: request.messages.length
      });
      
      // Handle non-streaming request
      const response = await coreWrapper.handleChatCompletion(request);
      
      logger.info('Chat completion request completed successfully', {
        requestId: response.id,
        model: response.model
      });

      res.json(response);
    }
  })
);

export default router;