import { PassThrough, Transform } from 'node:stream';
import type { AppLoadContext, EntryContext } from '@remix-run/node';
import { createReadableStreamFromReadable } from '@remix-run/node';
import { RemixServer } from '@remix-run/react';
import { isbot } from 'isbot';
import { renderToPipeableStream } from 'react-dom/server';
import { renderHeadToString } from 'remix-island';
import { Head } from './root';
import { themeStore } from '~/lib/stores/theme';

const ABORT_DELAY = 30_000;

export default async function handleRequest(
  request: Request,
  responseStatusCode: number,
  responseHeaders: Headers,
  remixContext: EntryContext,
  _loadContext: AppLoadContext,
) {
  const readyOption = isbot(request.headers.get('user-agent') || '') ? 'onAllReady' : 'onShellReady';

  return new Promise<Response>((resolve, reject) => {
    let renderedStatusCode = responseStatusCode;

    const { pipe, abort } = renderToPipeableStream(
      <RemixServer context={remixContext} url={request.url} abortDelay={ABORT_DELAY} />,
      {
        [readyOption]() {
          const head = renderHeadToString({ request, remixContext, Head });

          let shellStarted = false;
          const passthrough = new PassThrough();
          const wrapped = new Transform({
            transform(chunk, _encoding, callback) {
              if (!shellStarted) {
                this.push(
                  new TextEncoder().encode(
                    `<!DOCTYPE html><html lang="en" data-theme="${themeStore.value}"><head>${head}</head><body><div id="root" class="w-full h-full">`,
                  ),
                );
                shellStarted = true;
              }

              callback(null, chunk);
            },
            flush(callback) {
              this.push(new TextEncoder().encode('</div></body></html>'));
              callback();
            },
          });

          passthrough.pipe(wrapped);
          pipe(passthrough);

          const stream = createReadableStreamFromReadable(wrapped);

          responseHeaders.set('Content-Type', 'text/html');
          responseHeaders.set('Cross-Origin-Embedder-Policy', 'require-corp');
          responseHeaders.set('Cross-Origin-Opener-Policy', 'same-origin');

          resolve(
            new Response(stream, {
              headers: responseHeaders,
              status: renderedStatusCode,
            }),
          );

          // Prevent a suspended render from hanging the server forever.
          setTimeout(abort, ABORT_DELAY);
        },
        onError(error: unknown) {
          console.error(error);
          renderedStatusCode = 500;
        },
        onShellError(error: unknown) {
          reject(error);
        },
      } as Parameters<typeof renderToPipeableStream>[1],
    );
  });
}
