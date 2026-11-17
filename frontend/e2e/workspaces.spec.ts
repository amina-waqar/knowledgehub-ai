import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const PASSWORD = 'e2e-user-password'

async function signUp(request: APIRequestContext, username: string) {
  const response = await request.post('/auth/signup', {
    data: { username, email: `${username}@example.com`, password: PASSWORD },
  })
  expect(response.status()).toBe(201)
}

async function logIn(page: Page, username: string) {
  await page.goto('/')
  await page.getByLabel('Username').fill(username)
  await page.getByLabel('Password').fill(PASSWORD)
  await page.getByRole('button', { name: /Enter workspace/ }).click()
  await expect(page.getByLabel('Workspace', { exact: true })).toBeEnabled()
}

async function createIncident(page: Page, title: string) {
  await page.getByLabel('Title', { exact: true }).fill(title)
  await page.getByRole('button', { name: 'Create Incident' }).click()
  await expect(page.getByRole('heading', { level: 1, name: title })).toBeVisible()
  await page.getByRole('button', { name: '← Back to incidents' }).click()
}

async function switchWorkspace(page: Page, name: string) {
  await page.getByLabel('Workspace', { exact: true }).selectOption({ label: name })
  await expect(page.locator('.workspace-context')).toContainText(name)
}

async function apiAs(page: Page, request: APIRequestContext) {
  const token = await page.evaluate(() => localStorage.getItem('token'))
  return (method: 'GET' | 'POST', url: string, workspaceId?: number, data?: object) =>
    request.fetch(url, {
      method,
      data,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(workspaceId ? { 'X-Workspace-Id': String(workspaceId) } : {}),
      },
    })
}

test('incidents stay private to a workspace until it is shared', async ({ browser, request }) => {
  await signUp(request, 'alice')
  await signUp(request, 'bob')

  const alice = await (await browser.newContext()).newPage()
  const bob = await (await browser.newContext()).newPage()

  // Alice works in her personal workspace.
  await logIn(alice, 'alice')
  await expect(alice.locator('.workspace-context')).toContainText("alice's workspace")
  await createIncident(alice, 'Alice checkout outage')
  await expect(alice.getByText('Alice checkout outage')).toBeVisible()

  // Bob cannot see it in the UI or through the API.
  await logIn(bob, 'bob')
  await expect(bob.getByText('No incidents found.')).toBeVisible()
  await expect(bob.getByText('Alice checkout outage')).toHaveCount(0)

  const aliceApi = await apiAs(alice, request)
  const bobApi = await apiAs(bob, request)
  const aliceIncidents = await (await aliceApi('GET', '/incidents')).json()
  const privateIncidentId = aliceIncidents.incidents[0].id
  expect((await bobApi('GET', `/incidents/${privateIncidentId}`)).status()).toBe(404)

  // Alice creates a team workspace; its incidents are separate from her personal ones.
  await alice.getByRole('button', { name: '+ New workspace' }).click()
  await alice.getByLabel('New workspace name').fill('Payments team')
  await alice.getByRole('button', { name: 'Create', exact: true }).click()
  await expect(alice.locator('.workspace-context')).toContainText('Payments team')
  await expect(alice.getByText('No incidents found.')).toBeVisible()
  await createIncident(alice, 'Card declines')

  await switchWorkspace(alice, "alice's workspace")
  await expect(alice.getByText('Alice checkout outage')).toBeVisible()
  await expect(alice.getByText('Card declines')).toHaveCount(0)

  // Alice shares the team workspace with Bob as a viewer.
  await switchWorkspace(alice, 'Payments team')
  await alice.getByRole('button', { name: 'Workspace', exact: true }).click()
  await alice.getByLabel('Username').fill('bob')
  await alice.getByLabel('Role').selectOption('viewer')
  await alice.getByRole('button', { name: 'Add Member' }).click()
  await expect(alice.getByRole('heading', { name: 'bob' })).toBeVisible()

  // Bob now sees the team workspace read-only, and still not Alice's personal incident.
  await bob.reload()
  await switchWorkspace(bob, 'Payments team')
  await expect(bob.getByText('Card declines')).toBeVisible()
  await expect(bob.getByText('You have view-only access to this workspace.')).toBeVisible()
  await expect(bob.getByText('Alice checkout outage')).toHaveCount(0)

  const teamId = (await (await bobApi('GET', '/workspaces')).json()).workspaces.find(
    (workspace: { name: string }) => workspace.name === 'Payments team'
  ).id
  expect((await bobApi('POST', '/incidents', teamId, { title: 'Sneaky' })).status()).toBe(403)

  // Removing Bob takes the workspace away again.
  await alice.getByRole('button', { name: 'Remove bob' }).click()
  await expect(alice.getByRole('heading', { name: 'bob' })).toHaveCount(0)

  await bob.reload()
  await expect(bob.locator('.workspace-context')).toContainText("bob's workspace")
  await expect(bob.getByLabel('Workspace', { exact: true }).locator('option')).toHaveText(["bob's workspace"])
  expect((await bobApi('GET', '/incidents', teamId)).status()).toBe(404)
})
