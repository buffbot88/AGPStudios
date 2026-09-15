import { describe, expect, it } from 'vitest';
import SwitchableStream from './switchable-stream';

describe('SwitchableStream', () => {
  it('can replace a source while its reader is active', async () => {
    const first = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new Uint8Array([1])); } });
    const second = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new Uint8Array([2])); controller.close(); } });
    const stream = new SwitchableStream();
    const output = stream.readable.getReader();

    await stream.switchSource(first);
    await stream.switchSource(second);

    expect((await output.read()).value?.[0]).toBe(1);
    expect((await output.read()).value?.[0]).toBe(2);
  });
});
