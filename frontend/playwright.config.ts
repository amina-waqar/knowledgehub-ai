import os from 'node:os'
import path from 'node:path'

import { defineConfig, devices } from '@playwright/test'

const PORT = 8765
const python =
  process.env.E2E_PYTHON ??
  (process.platform === 'win32' ? 'venv\\Scripts\\python.exe' : 'venv/bin/python')

// A brand-new SQLite file for every run, outside the project, so the real
// database is never touched.
const databaseFile = path
  .join(os.tmpdir(), `loglens-e2e-${Date.now()}.db`)
  .replace(/\\/g, '/')

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `${python} migrate.py && ${python} -m uvicorn main:app --host 127.0.0.1 --port ${PORT}`,
    cwd: '..',
    url: `http://127.0.0.1:${PORT}/health`,
    reuseExistingServer: false,
    timeout: 180_000,
    env: {
      ...process.env,
      DATABASE_URL: `sqlite:///${databaseFile}`,
      SECRET_KEY: 'e2e-only-secret-key',
      LLM_PROVIDER: 'mock',
      // Set explicitly so values from the real .env are never used.
      BOOTSTRAP_ADMIN_USERNAME: 'e2e_admin',
      BOOTSTRAP_ADMIN_EMAIL: 'e2e_admin@example.com',
      BOOTSTRAP_ADMIN_PASSWORD: 'e2e-admin-password',
      DEMO_USERNAME: 'e2e_demo',
      DEMO_EMAIL: 'e2e_demo@example.com',
      DEMO_PASSWORD: 'e2e-demo-password',
    },
  },
})
