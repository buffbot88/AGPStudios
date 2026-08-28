import { env } from 'node:process';

/**
 * Galileo reads gateway config from the environment (Node on Alpha).
 * Defaults target alpha-server on loopback, the Ashat orchestrator
 * that routes coding generations to Omega/Beta/Delta.
 *
 * NOTE: reads must go through the native `node:process` module — the
 * node-polyfills rollup inject replaces the bare `process` global with a
 * browser shim whose `env` is always empty.
 */
export function getGatewayURL() {
  return env.ASHAT_GATEWAY_URL || 'http://127.0.0.1:3000';
}

export function getAPIKey() {
  return env.ASHAT_API_KEY || '';
}
