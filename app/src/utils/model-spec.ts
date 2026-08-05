/**
 * Model spec parsing
 *
 * Clients that can only send a model name — an OpenAI-compatible chat UI whose
 * dropdown is the single control surface — can encode the Claude CLI options
 * into it, colon separated:
 *
 *   opus                     model only
 *   opus:high                model + effort
 *   opus:high:plan           model + effort + permission mode
 *   opus:plan                model + permission mode (order independent)
 *
 * Segments are classified by membership rather than position, so the order of
 * the effort and permission-mode segments does not matter. The two vocabularies
 * do not overlap, which keeps the classification unambiguous.
 */

import { CLAUDE_EFFORTS, CLAUDE_PERMISSION_MODES } from './permission-mode';
import { logger } from './logger';

export const MODEL_SPEC_SEPARATOR = ':';

export interface ModelSpec {
  /** The bare model name, with any option segments removed. */
  model: string;
  effort?: string;
  permissionMode?: string;
  /** Segments that matched neither vocabulary; preserved for logging. */
  unknownSegments: string[];
}

const EFFORTS = new Set<string>(CLAUDE_EFFORTS);
const PERMISSION_MODES = new Set<string>(CLAUDE_PERMISSION_MODES);

/** Case-insensitive lookup that returns the canonical spelling. */
function matchCanonical(segment: string, vocabulary: Set<string>): string | undefined {
  const lowered = segment.toLowerCase();

  for (const candidate of vocabulary) {
    if (candidate.toLowerCase() === lowered) {
      return candidate;
    }
  }

  return undefined;
}

/**
 * Split a model string into its bare model name and any encoded CLI options.
 * A plain model name round-trips unchanged, so this is safe to run on every
 * request regardless of whether the client uses the encoding.
 */
export function parseModelSpec(rawModel: string | undefined): ModelSpec {
  if (!rawModel || typeof rawModel !== 'string') {
    return { model: rawModel ?? '', unknownSegments: [] };
  }

  const [model, ...segments] = rawModel.split(MODEL_SPEC_SEPARATOR);
  const spec: ModelSpec = { model: (model ?? '').trim(), unknownSegments: [] };

  for (const rawSegment of segments) {
    const segment = rawSegment.trim();

    if (!segment) {
      continue;
    }

    const effort = matchCanonical(segment, EFFORTS);
    if (effort && spec.effort === undefined) {
      spec.effort = effort;
      continue;
    }

    const permissionMode = matchCanonical(segment, PERMISSION_MODES);
    if (permissionMode && spec.permissionMode === undefined) {
      spec.permissionMode = permissionMode;
      continue;
    }

    spec.unknownSegments.push(segment);
  }

  if (spec.unknownSegments.length > 0) {
    logger.warn('Ignoring unrecognized model spec segments', {
      rawModel,
      unknownSegments: spec.unknownSegments
    });
  }

  return spec;
}

/**
 * Build the `model:effort` variants advertised on /v1/models so a model
 * dropdown can drive effort. Permission mode is deliberately not enumerated:
 * the combinations multiply past what a dropdown can usefully show, and
 * bypassPermissions is not something to offer as a casual menu item. It is
 * still accepted as a third segment for clients that ask for it explicitly.
 */
export function buildModelVariants(baseModelIds: string[]): string[] {
  const variants: string[] = [];

  for (const baseModel of baseModelIds) {
    for (const effort of CLAUDE_EFFORTS) {
      variants.push(`${baseModel}${MODEL_SPEC_SEPARATOR}${effort}`);
    }
  }

  return variants;
}
