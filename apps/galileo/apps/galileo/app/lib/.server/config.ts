import { readFileSync } from 'node:fs';
import { cwd } from 'node:process';

interface GalileoConfig {
  gateway?: {
    url?: string;
    api_key?: string;
  };
}

const DEFAULT_GATEWAY_URL = 'http://127.0.0.1:3000';
let cached: { gatewayURL: string; apiKey: string } | undefined;

/**
 * Loads config.json from the working directory once per process; a missing
 * file falls back to the built-in defaults while malformed JSON fails loudly.
 * Restart Galileo after editing the file.
 */
function loadConfig() {
  if (cached) {
    return cached;
  }

  const configPath = `${cwd().replace(/[\\/]+$/, '')}/config.json`;
  let fileConfig: GalileoConfig = {};

  try {
    fileConfig = JSON.parse(readFileSync(configPath, 'utf8')) as GalileoConfig;
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code !== 'ENOENT') {
      throw new Error(`Failed to read ${configPath}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  cached = {
    gatewayURL: process.env.ASHAT_GATEWAY_URL?.trim() || fileConfig.gateway?.url?.trim() || DEFAULT_GATEWAY_URL,
    apiKey: process.env.ASHAT_GATEWAY_API_KEY || fileConfig.gateway?.api_key || '',
  };

  return cached;
}

export function getGatewayURL() {
  return loadConfig().gatewayURL;
}

export function getAPIKey() {
  return loadConfig().apiKey;
}
