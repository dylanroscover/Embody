import { test, expect, type Page } from "@playwright/test";

async function selectOp(page: Page, name: string): Promise<void> {
  await page
    .locator(".tdxn-operator", { has: page.locator(".tdxn-operator__name", { hasText: new RegExp(`^${name}$`) }) })
    .click();
  await expect(page.locator(".prop-head__name")).toHaveText(name);
}

// The value field of the properties row whose key reads exactly `key`.
function propValue(page: Page, key: string) {
  return page
    .locator(".prop-row", { has: page.locator(".prop-row__key", { hasText: new RegExp(`^${key}$`) }) })
    .locator(".prop-row__val");
}

// A parameter value wider than the properties panel stays reachable: the field
// scrolls sideways (drag-select, shift+wheel, trackpad, touch) and hovering it
// shows the whole value in a tooltip. It used to clip to an ellipsis with only a
// slow native title behind it (field report 2026-10-19). tile-zoom-type's
// text_glyph carries a long `text` expression at the root level.
test("a clipped expression scrolls and shows whole in a hover tooltip", async ({ page }) => {
  await page.goto("/c/tile-zoom-type");
  await selectOp(page, "text_glyph");

  const field = page.locator(".prop-row__val.is-expr", { hasText: "parent.Specimen.par.Letters" });
  await expect(field).toBeVisible();
  const full = (await field.textContent()) ?? "";
  expect(await field.evaluate((e) => e.scrollWidth > e.clientWidth), "the value must overflow its field").toBe(true);

  // A horizontal wheel scrolls the field; an overflow:hidden field ignores it.
  await field.hover();
  await page.mouse.wheel(400, 0);
  await expect.poll(() => field.evaluate((e) => e.scrollLeft)).toBeGreaterThan(0);

  // The tooltip holds the entire value, unclipped.
  const tip = page.locator(".prop-tip");
  await expect(tip).toBeVisible();
  await expect(tip).toHaveText(full);
  expect(await tip.evaluate((e) => e.scrollWidth <= e.clientWidth && e.scrollHeight <= e.clientHeight)).toBe(true);

  // Leaving the field hides it again.
  await page.mouse.move(5, 5);
  await expect(tip).toBeHidden();
});

// Every parameter the TDXN stores shows, not just the op's own `parameters`
// block: sequences (GLSL uniforms), the type's shared `type_defaults`, `$t`
// template pages, and -- with nothing selected -- the network's own COMP.
test("the panel shows uniforms, type defaults and the network's own pars", async ({ page }) => {
  await page.goto("/c/noise-terrain");

  // Nothing selected: the specimen COMP itself, with its custom pars.
  await expect(page.locator(".prop-head__name")).toHaveText("noise_terrain");
  await expect(propValue(page, "Height")).toHaveText("1");

  // GLSL uniforms live in the `vec` sequence, under TD's own par names.
  await selectOp(page, "glsl_snow");
  await expect(propValue(page, "vec0name")).toHaveText("uTerrain");
  await expect(propValue(page, "vec1valuex")).toHaveText(
    "=math.cos(math.radians(parent().par.Sundirel.eval()))*math.sin(math.radians(parent().par.Sundiraz.eval()))"
  );

  // `language: glsl` is shared by every textDAT, so it lives in type_defaults.
  await selectOp(page, "glsl_snow_pixel");
  await expect(propValue(page, "language")).toHaveText("glsl");
});

test("a template-referenced custom page shows its values", async ({ page }) => {
  await page.goto("/c/mandala");
  await selectOp(page, "bead_ring");
  await expect(propValue(page, "Shape")).toHaveText("dot");
  await expect(propValue(page, "Draw Order")).toHaveText("7");
});
