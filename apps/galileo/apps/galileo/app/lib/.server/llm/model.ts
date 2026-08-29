import { createOpenAI } from '@ai-sdk/openai';
// @ts-expect-error The package exposes the runtime ponyfill without a matching subpath declaration.
import { TransformStream } from 'web-streams-polyfill/dist/ponyfill.js';

class CompatibleTextDecoderStream {
  readable: ReadableStream<string>;
  writable: WritableStream<Uint8Array>;

  constructor() {
    const decoder = new TextDecoder();
    const stream = new TransformStream<Uint8Array, string>({
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

// The AI SDK response parser uses the Web Streams polyfill. Node 24 exposes
// native stream constructors globally, which are not interchangeable.
Object.assign(globalThis, { TextDecoderStream: CompatibleTextDecoderStream, TransformStream });

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
