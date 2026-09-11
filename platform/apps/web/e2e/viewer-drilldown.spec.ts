import { test, expect } from "@playwright/test";

// The specimen-page TdxnViewer is `navigable`: a single click selects an operator
// (surfacing its parameters); a COMP tile is DOUBLE-clicked to enter (drilling
// into its sub-network), with a breadcrumb "address bar" to climb back out.
// noise-terrain is the seeded specimen with
// nested COMPs (cam/geo/light), so it exercises the whole walk -- root level,
// descend, and return. The cover-graph thumbnails stay flattened/non-navigable
// (asserted in collection.spec.ts), so only this page-level viewer is checked.

test("specimen viewer drills into a COMP and climbs back out", async ({ page }) => {
  await page.goto("/c/noise-terrain");

  const viewer = page.locator(".tdxn-viewer");
  const breadcrumb = viewer.locator(".tdxn-viewer__breadcrumb");
  const crumbs = breadcrumb.locator(".tdxn-crumb");
  const opNames = () =>
    viewer.locator(".tdxn-operator__name").evaluateAll((els) =>
      els.map((e) => (e.textContent || "").trim())
    );

  // At root: the breadcrumb shows exactly one crumb (the network root) and at
  // least one COMP advertises that it can be entered.
  await expect(breadcrumb).toBeVisible();
  await expect(crumbs).toHaveCount(1);
  const enterable = viewer.locator(".tdxn-operator--enterable").first();
  await expect(enterable).toBeVisible();

  const compName = (await enterable.locator(".tdxn-operator__name").textContent())?.trim();
  expect(compName, "an enterable COMP must have a name").toBeTruthy();
  const rootOps = await opNames();

  // Double-click the UNSELECTED COMP -> descend into its sub-network. Never
  // select it first: the dblclick's 1st click must change the selection, which
  // once rebuilt every node and hid the tiles for a frame, so the 2nd click
  // missed the COMP (field 2026-09-11). A pre-click masks that regression.
  await enterable.dblclick();

  // Breadcrumb gains a second crumb naming the COMP we entered, and the visible
  // operator set is now that COMP's children (different from the root level).
  await expect(crumbs).toHaveCount(2);
  await expect(crumbs.nth(1)).toHaveText(compName!);
  await expect(viewer.locator(".tdxn-operator__name").first()).toBeVisible();
  const childOps = await opNames();
  expect(childOps).not.toEqual(rootOps);

  // Click the root crumb -> climb back out to the top-level network.
  await crumbs.first().click();
  await expect(crumbs).toHaveCount(1);
  await expect.poll(opNames).toEqual(rootOps);

  // One click selects the COMP: the ring reads SelectedOpContext, not node data.
  await expect(enterable).not.toHaveClass(/tdxn-operator--selected/);
  await enterable.click();
  await expect(enterable).toHaveClass(/tdxn-operator--selected/);
});
