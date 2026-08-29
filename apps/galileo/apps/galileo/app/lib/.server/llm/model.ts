import { createOpenAI } from '@ai-sdk/openai';
// @ts-expect-error The runtime bundle exports the constructors without subpath declarations.
import * as WebStreamsPolyfill from 'web-streams-polyfill/dist/polyfill.js';

class CompatibleTextDecoderStream {
  readable: ReadableStream<string>;
  writable: WritableStream<Uint8Array>;

  constructor() {
    const decoder = new TextDecoder();
    const stream = new WebStreamsPolyfill.TransformStream<Uint8Array, string>({
      transform(chunk: Uint8Array, controller: TransformStreamDefaultController<string>) {
        controller.enqueue(decoder.decode(chunk, { stream: true }));
      },
      flush(controller: TransformStreamDefaultController<string>) {
        const remainder = decoder.decode();
        if (remainder) controller.enqueue(remainder);
      },
    });
    this.readable = stream.readable;
    this.writable = stream.writable;
  }
}

// The AI SDK response parser uses the Web Streams polyfill. Force its
// constructors globally so Remix and the provider parser share their identity.
Object.assign(globalThis, WebStreamsPolyfill, {
  TextDecoderStream: CompatibleTextDecoderStream,
});

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
