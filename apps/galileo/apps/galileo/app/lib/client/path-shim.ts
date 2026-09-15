import pathBrowserify from 'path-browserify';

// Named ESM re-exports of path-browserify. Source files import
// `* as nodePath from 'node:path'`; the dev-optimizer's CJS interop does not
// expose named exports for namespace imports, so client code resolves `path`
// through this shim instead (see the `path` alias in vite.config.ts).
// path-browserify's exports are standalone closures (no internal `this`), so
// destructuring is safe.
export const {
  resolve,
  normalize,
  isAbsolute,
  join,
  relative,
  dirname,
  basename,
  extname,
  format,
  parse,
  sep,
  delimiter,
  posix,
  win32,
} = pathBrowserify;
export default pathBrowserify;
