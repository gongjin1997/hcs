import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
    // Bound component compilation on shared builders without relaxing timeouts.
    maxWorkers: 4,
  },
})
