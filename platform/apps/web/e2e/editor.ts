import { type Page } from "@playwright/test";

// The contribute/edit TDN field is a CodeMirror 6 editor (TdnYamlEditor) backed
// by a HIDDEN <textarea name="tdn"> (kept only for the form's FormData contract).
// You therefore cannot `.fill('textarea[name="tdn"]')` -- the textarea is
// display:none, so Playwright's fill() never sees an editable element and times
// out. Drive the editor's contenteditable instead; CodeMirror syncs the value
// back to the hidden textarea and into the submit-gating validity state.
export async function fillTdn(page: Page, value: string): Promise<void> {
  const content = page.locator(".tdn-editor__cm .cm-content");
  await content.click();
  // Select-all + insert replaces any existing content (no-op when empty).
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.insertText(value);
  // Let CodeMirror's update listener flush to the textarea + validity.
  await page.waitForTimeout(50);
}
