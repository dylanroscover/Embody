// Faithful TDN v2.0 sample network. TDN files are YAML on disk; this fixture is
// the parsed object the TdnViewer renders and that [slug].astro serializes for the
// "raw TDN" panel. Keys, ordering, and value shorthand mirror a real v2.0 export
// (see specimens/simulation/murmuration.tdn).
export const sampleTdn = {
  format: "tdn",
  version: "2.0",
  build: null,
  generator: "Embody/6.0.16",
  td_build: "099.2025.32820",
  source_file: "Embody-6.16.toe",
  exported_at: "2026-06-12T00:27:58Z",
  network_path: "/project1/infinite_zoom_tunnel",
  type: "baseCOMP",
  options: {
    include_dat_content: true,
    include_storage: true
  },
  color: [0.22, 0.5, 0.32],
  operators: [
    {
      name: "noise_source",
      type: "noiseTOP",
      parameters: {
        resolutionw: 1280,
        resolutionh: 720,
        type: "sparse",
        period: 7.5,
        harmonics: 5,
        amp: 0.82
      },
      position: [0, 200],
      size: [130, 90],
      color: [0.16, 0.72, 0.38]
    },
    {
      name: "feedback1",
      type: "feedbackTOP",
      parameters: {
        targettop: "level1"
      },
      position: [0, -100],
      size: [130, 90],
      color: [0.16, 0.72, 0.38],
      inputs: ["level1"]
    },
    {
      name: "transform1",
      type: "transformTOP",
      parameters: {
        scale: 1.018,
        rotate: 0.34,
        tx: 0.002,
        ty: -0.001
      },
      position: [300, -100],
      size: [130, 90],
      inputs: ["feedback1"]
    },
    {
      name: "composite1",
      type: "compositeTOP",
      parameters: {
        operand: "add",
        opacity: 0.58
      },
      position: [600, 100],
      size: [130, 90],
      inputs: ["noise_source", "transform1"]
    },
    {
      name: "level1",
      type: "levelTOP",
      parameters: {
        blacklevel: 0.045,
        brightness: 1.04,
        gamma: 0.91
      },
      position: [900, 100],
      size: [130, 90],
      color: [0.62, 0.55, 0.16],
      inputs: ["composite1"]
    },
    {
      name: "out",
      type: "nullTOP",
      parameters: {
        cooktype: "selective"
      },
      position: [1200, 100],
      size: [130, 90],
      color: [0.16, 0.72, 0.38],
      inputs: ["level1"]
    }
  ],
  annotations: [
    {
      name: "feedback_note",
      mode: "annotate",
      title: "Feedback tunnel core",
      text: "The loop reads the graded result, scales it, and composites it back over the source.",
      position: [-90, -220],
      size: [1390, 580],
      color: [0.45, 0.45, 0.45],
      opacity: 0.55
    }
  ]
} satisfies Record<string, unknown>;
