# Canonical agent contract

Galileo, Alpha, Omega, Beta, and Delta migrate toward one agent request and event stream. Chat/Plan/Build modes are not part of this contract.

## Request

```ts
 type AgentRequest = {
  conversation_id: string;
  project_id?: string;
  messages: AgentMessage[];
  operation?: 'chat' | 'agent' | 'vision';
  capabilities: { tools: boolean; vision: boolean };
};
```

## Events

The stream uses `response.start`, `text.delta`, `tool.start`, `tool.arguments`, `tool.result`, `status`, `error`, and `response.complete`. Tool identity and arguments are event data; consumers must never parse assistant text to discover tools.

The canonical event fields are:

```ts
response.start   { response_id }
text.delta       { delta }
tool.start       { id, name }
tool.arguments   { id, arguments }
tool.result      { id, ok, result?, error? }
status           { state, message? }
error            { code, message, retryable }
response.complete {}
```

This file freezes the wire shape. Compatibility adapters may remain until every runtime uses it.
