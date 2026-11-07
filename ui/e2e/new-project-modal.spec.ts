import { test, expect } from '@playwright/test'

/**
 * E2E tests for the New Project dialog (create / import entry points).
 *
 * Run tests:
 *   cd ui && npm run test:e2e -- new-project-modal.spec.ts
 */
test.describe('New Project dialog', () => {
  test.setTimeout(30000)

  test.beforeEach(async ({ page }) => {
    await page.goto('/')
    const selector = page.locator('button:has-text("Select Project")')
    await selector.waitFor({ timeout: 10000 })
    await selector.click()
    await page.getByRole('menuitem', { name: 'New Project' }).click()
  })

  test('shows both entry points in a visible dialog', async ({ page }) => {
    const dialog = page.getByRole('dialog', { name: 'New Project' })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByText('Create New Project')).toBeVisible()
    await expect(dialog.getByText('Import Existing Project')).toBeVisible()
  })

  test('create path goes to the name step and back', async ({ page }) => {
    await page.getByRole('dialog').getByText('Create New Project').click()

    const nameDialog = page.getByRole('dialog', { name: 'Create New Project' })
    await expect(nameDialog.getByLabel('Project Name')).toBeVisible()

    await nameDialog.getByRole('button', { name: 'Back' }).click()
    await expect(page.getByRole('dialog', { name: 'New Project' })).toBeVisible()
  })

  test('project name pattern rejects invalid names', async ({ page }) => {
    // The pattern must be valid under the browser's /v regex flag, or it is ignored
    await page.getByRole('dialog').getByText('Create New Project').click()
    const nameInput = page.getByRole('dialog', { name: 'Create New Project' }).getByLabel('Project Name')

    await nameInput.fill('bad name!')
    expect(await nameInput.evaluate((el: HTMLInputElement) => el.validity.patternMismatch)).toBe(true)

    await nameInput.fill('good-name_1')
    expect(await nameInput.evaluate((el: HTMLInputElement) => el.validity.patternMismatch)).toBe(false)
  })

  test('import path opens the folder picker', async ({ page }) => {
    await page.getByRole('dialog').getByText('Import Existing Project').click()

    const importDialog = page.getByRole('dialog', { name: 'Import Existing Project' })
    await expect(importDialog).toBeVisible()
    await expect(importDialog.getByText('Select Project Folder')).toBeVisible()
  })
})
