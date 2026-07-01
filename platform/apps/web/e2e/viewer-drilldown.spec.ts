import { test, expect } from "@playwright/test";

// The specimen-page TdnViewer is `navigable`: COMPs render as single tiles you
// can click to enter (drilling into their sub-network), with a breadcrumb
// "address bar" to climb back out. noise-terrain is the seeded specimen with
// nested COMPs (cam/geo/light), so it exercises the whole walk -- root level,
// descend, and return. The cover-graph thumbnails stay flattened/non-navigable
// (asserted in collection.spec.ts), so only this page-level viewer is checked.

test("specimen viewer drills into a COMP and climbs back out", async ({ page }) => {
  await page.goto("/c/noise-terrain");

  const viewer = page.locator(".tdn-viewer");
  const breadcrumb = viewer.locator(".tdn-viewer__breadcrumb");
  const crumbs = breadcrumb.locator(".tdn-crumb");
  const opNames = () =>
    viewer.locator(".tdn-operator__name").evaluateAll((els) =>
      els.map((e) => (e.textContent || "").trim())
    );

  // At root: the breadcrumb shows exactly one crumb (the network root) and at
  // least one COMP advertises that it can be entered.
  await expect(breadcrumb).toBeVisible();
  await expect(crumbs).toHaveCount(1);
  const enterable = viewer.locator(".tdn-operator--enterable").first();
  await expect(enterable).toBeVisible();

  const compName = (await enterable.locator(".tdn-operator__name").textContent())?.trim();
  expect(compName, "an enterable COMP must have a name").toBeTruthy();
  const rootOps = await opNames();

  // Click the COMP -> descend into its sub-network.
  await enterable.click();

  // Breadcrumb gains a second crumb naming the COMP we entered, and the visible
  // operator set is now that COMP's children (different from the root level).
  await expect(crumbs).toHaveCount(2);
  await expect(crumbs.nth(1)).toHaveText(compName!);
  await expect(viewer.locator(".tdn-operator__name").first()).toBeVisible();
  const childOps = await opNames();
  expect(childOps).not.toEqual(rootOps);

  // Click the root crumb -> climb back out to the top-level network.
  await crumbs.first().click();
  await expect(crumbs).toHaveCount(1);
  await expect.poll(opNames).toEqual(rootOps);
});
