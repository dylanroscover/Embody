// The minimum supported TouchDesigner build for Embody -- the build the
// shipped .tox was last saved with. TD files do not open in older builds, so
// the save build IS the support floor.
//
// SINGLE SOURCE for the site. The MIN_TD_BUILD line is rewritten
// automatically on every project save by execute_src_ctrl.updateVersionDocs
// (the same hook that keeps README.md / docs/index.md / CONTRIBUTING.md in
// lock-step) and guarded by test_version_sync. Never hand-edit it, and never
// restate the build as a literal anywhere else in the site -- import this.
export const MIN_TD_BUILD = "2025.33070";
