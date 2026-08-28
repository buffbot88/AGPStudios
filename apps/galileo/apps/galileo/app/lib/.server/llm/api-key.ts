/**
 * Galileo reads gateway config from process.env (Node on Alpha).
 * Defaults target alpha-server on loopback, the Ashat orchestrator
 * that routes coding generations to Omega/Beta/Delta.
 */
export function getGatewayURL() {
  return process.env.ASHAT_GATEWAY_URL || 'http://127.0.0.1:3000';
}

export function getAPIKey() {
  return process.env.ASHAT_API_KEY || '';
}
