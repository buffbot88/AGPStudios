import { createOpenAI } from '@ai-sdk/openai';
/**
 * Builds a model backed by the Ashat Hub gateway. The gateway classifies
 * each request and routes it to the Omega/Beta/Delta agent pool.
 */
export function getGatewayModel(gatewayUrl: string, apiKey: string) {
  const base = gatewayUrl.trim().replace(/\/+$/, '');
  const baseURL = base.endsWith('/v1') ? base : `${base}/v1`;

  const gateway = createOpenAI({
    baseURL,
    apiKey: apiKey || 'galileo',
  });

  return gateway('ashat');
}
