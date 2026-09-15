// Alpha caps local 350M output at 1024 and remote coding output at 4096.
export const MAX_TOKENS = 4096;

// Slightly outlast Alpha's 180s remote request timeout.
export const GATEWAY_TIMEOUT_MS = 210_000;

// limits the number of model responses that can be returned in a single request
export const MAX_RESPONSE_SEGMENTS = 2;
