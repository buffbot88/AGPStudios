import { json } from '@remix-run/node';

export type GatewayErrorCode = 'gateway_unreachable' | 'queue_full' | 'agent_timeout' | 'inference_failed';

/**
 * Maps gateway/provider failures to a stable JSON error envelope that the
 * client turns into friendly toasts.
 */
export function gatewayErrorResponse(error: unknown, requestId?: string) {
  const statusCode = (error as { statusCode?: number } | null)?.statusCode;
  const text = error instanceof Error ? `${error.name} ${error.message}` : String(error);

  if (statusCode === 429) {
    return envelope(429, 'queue_full', 'The Ashat inference queue is full.', requestId);
  }

  if (/timed?\s?out|timeout|etimedout|abort/i.test(text)) {
    return envelope(504, 'agent_timeout', 'The coding agent took too long to respond.', requestId);
  }

  if (/econnrefused|econnreset|enotfound|eai_again|epipe|fetch failed|connection (error|refused|closed)/i.test(text)) {
    return envelope(503, 'gateway_unreachable', 'The Ashat gateway is unreachable.', requestId);
  }

  if (statusCode === 502 || statusCode === 503) {
    return envelope(502, 'inference_failed', 'Inference failed on the Omega/Beta/Delta agent pool.', requestId);
  }

  return envelope(500, 'inference_failed', 'Chat inference failed.', requestId);
}

function envelope(status: number, code: GatewayErrorCode, message: string, requestId?: string) {
  return json<{ error: { code: GatewayErrorCode; message: string }; request_id?: string }>(
    { error: { code, message }, ...(requestId ? { request_id: requestId } : {}) },
    { status, headers: requestId ? { 'X-Request-Id': requestId } : undefined },
  );
}
