import type { Config } from "@react-router/dev/config";
import { cp, mkdir, readdir, rm, stat } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const normalizeBasePath = (value?: string) => {
  const raw = value?.trim();
  if (!raw || raw === "/") return undefined;

  const withLeadingSlash = raw.startsWith("/") ? raw : `/${raw}`;
  return withLeadingSlash.replace(/\/+$/, "");
};

const basename = normalizeBasePath(process.env.VITE_BASE_PATH);

// React Router v7 SPA mode drops the built client into `build/client/`. The
// minio-ork prod layout wants the static assets next to the FastAPI app so
// `mini-ork serve` can serve them directly — no separate deployment step.
// The previous OpenHands layout used a buildEnd hook to flatten `build/`
// (Vercel-style); the fork keeps the hook shape but redirects the destination
// to `../mini_ork/web/static/`.
const publishStaticAssets = async () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const builtClient = resolve(here, "build", "client");
  const target = resolve(here, "..", "mini_ork", "web", "static");

  try {
    const s = await stat(builtClient);
    if (!s.isDirectory()) return;
  } catch {
    return;
  }

  await mkdir(target, { recursive: true });
  await rm(target, { recursive: true, force: true });
  await mkdir(target, { recursive: true });

  const entries = await readdir(builtClient, { withFileTypes: true });
  for (const entry of entries) {
    const src = resolve(builtClient, entry.name);
    const dest = resolve(target, entry.name);
    if (entry.isDirectory()) {
      await cp(src, dest, { recursive: true, force: true });
    } else {
      await cp(src, dest, { force: true });
    }
  }
};

export default {
  appDirectory: "src",
  ...(basename ? { basename } : {}),
  buildEnd: publishStaticAssets,
  ssr: false,
} satisfies Config;
