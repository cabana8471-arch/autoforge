import { test, expect, type APIRequestContext, type Page } from '@playwright/test'

/**
 * E2E tests for re-registering an AutoForge folder through the import wizard
 * and for the Backlog lane (park, ready checks, promote).
 *
 * The tests create their own projects through the API in E2E_PROJECTS_DIR, an
 * absolute folder outside /tmp (the server's folder browser refuses /tmp), and
 * unregister them afterwards. Use a test server with its own HOME so the real
 * registry is untouched (memory: local-test-recipe).
 *
 * Run tests:
 *   E2E_PROJECTS_DIR=$HOME/.cache/af-e2e/projects VITE_API_PORT=8899 \
 *     npx playwright test e2e/import-and-backlog.spec.ts
 */
const ROOT = process.env.E2E_PROJECTS_DIR ?? ''

test.skip(!ROOT, 'E2E_PROJECTS_DIR is not set')

function uniqueName(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}${Math.floor(Math.random() * 1000)}`
}

async function createProject(request: APIRequestContext, name: string): Promise<string> {
  const path = `${ROOT}/${name}`
  const res = await request.post('/api/projects', { data: { name, path, spec_method: 'manual' } })
  expect(res.ok(), await res.text()).toBeTruthy()
  return path
}

async function createFeature(
  request: APIRequestContext,
  project: string,
  name: string,
  extra: Record<string, unknown> = {},
): Promise<number> {
  const res = await request.post(`/api/projects/${project}/features`, {
    data: {
      category: 'E2E',
      name,
      description: `${name} does what the e2e test expects.`,
      steps: ['Open the page', 'Check the result'],
      ...extra,
    },
  })
  expect(res.ok(), await res.text()).toBeTruthy()
  return (await res.json()).id
}

async function unregister(request: APIRequestContext, name: string): Promise<void> {
  await request.delete(`/api/projects/${name}`)
}

async function selectProject(page: Page, name: string): Promise<void> {
  await page.goto('/')
  const selector = page.locator('button:has-text("Select Project")')
  await selector.waitFor({ timeout: 10000 })
  await selector.click()
  await page.getByRole('menuitem', { name: new RegExp(`^${name}`) }).click()
  await openKanban(page)
}

async function openKanban(page: Page): Promise<void> {
  await page.getByRole('button', { name: /^Kanban/ }).click()
  await expect(column(page, 'Pending')).toBeVisible({ timeout: 10000 })
}

function column(page: Page, title: string) {
  return page.getByTestId(`kanban-column-${title.toLowerCase().replace(/\s+/g, '-')}`)
}

test.describe('Import wizard: re-register an AutoForge folder', () => {
  test.setTimeout(60000)

  test('keeps the existing spec and features', async ({ page, request }) => {
    const original = uniqueName('e2e-orig')
    const reregistered = uniqueName('e2e-rereg')
    const path = await createProject(request, original)
    await createFeature(request, original, 'Kept feature')
    await unregister(request, original)

    try {
      await page.goto('/')
      const selector = page.locator('button:has-text("Select Project")')
      await selector.waitFor({ timeout: 10000 })
      await selector.click()
      await page.getByRole('menuitem', { name: 'New Project' }).click()
      await page.getByRole('dialog').getByText('Import Existing Project').click()

      const pathInput = page.getByLabel('Folder path')
      await pathInput.fill(path)
      await pathInput.press('Enter')
      await page.getByRole('button', { name: 'Select This Folder' }).click()

      await expect(page.getByText('This folder already contains AutoForge data')).toBeVisible({ timeout: 20000 })
      await page.getByRole('button', { name: 'Re-register as is' }).click()

      const dialog = page.getByRole('dialog', { name: 'Re-register Project' })
      await dialog.getByLabel('Project Name').fill(reregistered)
      await dialog.getByRole('button', { name: 'Re-register Project' }).click()
      await expect(page.getByText('Existing spec, features and progress kept')).toBeVisible()

      // Redirected to the project: the feature created before unregistering is still there
      await expect(page.getByRole('button', { name: new RegExp(`^${reregistered}`) })).toBeVisible({ timeout: 15000 })
      await openKanban(page)
      await expect(column(page, 'Pending').getByText('Kept feature', { exact: true })).toBeVisible({ timeout: 15000 })
      const projects = await (await request.get('/api/projects')).json()
      expect(projects.map((p: { name: string }) => p.name)).toContain(reregistered)
    } finally {
      await unregister(request, reregistered)
    }
  })
})

test.describe('Backlog lane', () => {
  test.setTimeout(60000)

  test('parks a feature outside progress and promotes it after review', async ({ page, request }) => {
    const project = uniqueName('e2e-backlog')
    await createProject(request, project)
    await createFeature(request, project, 'Ready feature')
    await createFeature(request, project, 'Parked feature')

    try {
      await selectProject(page, project)
      await column(page, 'Pending').getByText('Parked feature', { exact: true }).click()
      await page.getByRole('button', { name: 'Move to Backlog' }).click()

      // Parked: ready checks shown, maturity tag missing
      await expect(page.getByTestId('ready-checks')).toBeVisible()
      await expect(page.getByRole('button', { name: /Promote to Pending/ })).toHaveCount(0)

      // Out of the agent's progress totals while parked
      const parkedStats = await (await request.get(`/api/projects/${project}/stats`)).json()
      expect(parkedStats.total).toBe(1)

      await page.getByRole('radio', { name: 'Beta', exact: true }).click()
      await page.getByRole('button', { name: 'Save Review' }).click()
      await page.getByRole('button', { name: /Promote to Pending/ }).click()
      await page.keyboard.press('Escape')

      await expect(column(page, 'Pending').getByText('Parked feature', { exact: true })).toBeVisible()
      await expect(column(page, 'Backlog').getByText('Parked feature', { exact: true })).toHaveCount(0)
      const promotedStats = await (await request.get(`/api/projects/${project}/stats`)).json()
      expect(promotedStats.total).toBe(2)
    } finally {
      await unregister(request, project)
    }
  })
})
