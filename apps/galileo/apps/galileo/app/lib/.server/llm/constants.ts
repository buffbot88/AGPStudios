// The gateway clamps remote Omega/Beta/Delta requests to 4096 tokens.
export const MAX_TOKENS = 8192;

// Abort inference if the gateway/agent pool doesn't finish in time.
export const GATEWAY_TIMEOUT_MS = 300_000;

// limits the number of model responses that can be returned in a single request
export const MAX_RESPONSE_SEGMENTS = 2;
