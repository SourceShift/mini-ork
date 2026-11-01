// Bundle the extension host code. `vscode` is provided by the editor at
// runtime and must never be bundled; everything else (including `ws`, since
// VS Code's Node 18 has no global WebSocket) is inlined into one file so the
// VSIX needs no node_modules tree.
import * as esbuild from 'esbuild';

const production = process.argv.includes('--production');
const watch = process.argv.includes('--watch');
const tests = process.argv.includes('--tests');

const shared = {
  bundle: true,
  external: ['vscode', 'node:*'],
  platform: 'node',
  target: 'node18',
  sourcemap: !production,
  minify: production,
  logLevel: 'info',
};

if (tests) {
  // The package is CommonJS, but the test files use ESM syntax against the
  // TypeScript sources. Bundling them to `.mjs` lets `node --test` run them
  // without a transpiler in the loop.
  await esbuild.build({
    ...shared,
    entryPoints: { 'sse.test': 'test/sse.test.ts', 'discovery.test': 'test/discovery.test.ts' },
    outdir: 'dist/test',
    outExtension: { '.js': '.mjs' },
    format: 'esm',
  });
} else {
  const options = {
    ...shared,
    entryPoints: ['src/extension.ts'],
    outfile: 'dist/extension.js',
    format: 'cjs',
  };

  if (watch) {
    const ctx = await esbuild.context(options);
    await ctx.watch();
  } else {
    await esbuild.build(options);
  }
}
