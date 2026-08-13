/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** X-Portal-Token shared secret (DESIGN §Portal API). Read from build-time
   * env, never hardcoded in source — see `.env.example` at the portal root. */
  readonly VITE_PORTAL_TOKEN?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
