export interface GatewayErrorEnvelope {
  error?: {
    code?: 'gateway_unreachable' | 'queue_full' | 'agent_timeout' | 'inference_failed';
    message?: string;
  };
}

const FRIENDLY_MESSAGES: Record<string, string> = {
  gateway_unreachable: "Can't reach the Ashat gateway. Make sure alpha-server is running on Alpha (port 3000), then try again.",
  queue_full: 'Ashat is busy right now — the inference queue is full. Try again in a few seconds.',
  agent_timeout: 'The coding agent took too long to respond. Try again, or simplify the request.',
  inference_failed: 'Inference failed on the Omega/Beta/Delta agent pool. Please try again.',
};

/**
 * Turns a chat/enhancer failure into user-friendly copy. Server errors arrive
 * as the raw JSON envelope in the error message; network failures arrive as
 * plain Errors.
 */
export function friendlyChatErrorMessage(error: unknown) {
  let code: string | undefined;
  let message: string | undefined;

  if (error instanceof Error) {
    try {
      const parsed = JSON.parse(error.message) as GatewayErrorEnvelope;
      code = parsed.error?.code;
      message = parsed.error?.message;
    } catch {
      message = error.message;
    }
  } else if (error && typeof error === 'object') {
    const envelope = error as GatewayErrorEnvelope;
    code = envelope.error?.code;
    message = envelope.error?.message;
  }

  if (code && FRIENDLY_MESSAGES[code]) {
    return FRIENDLY_MESSAGES[code];
  }

  return message || 'Something went wrong talking to Ashat. Please try again.';
}
