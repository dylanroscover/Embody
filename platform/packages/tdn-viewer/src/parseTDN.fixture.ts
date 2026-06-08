export const parseTDNFixture = {
  format: "tdn",
  version: "1.4",
  network_path: "/project1/feedback_tunnel",
  type: "baseCOMP",
  operators: [
    {
      name: "noise1",
      type: "noiseTOP",
      position: [0, 200],
      color: [0.16, 0.72, 0.38]
    },
    {
      name: "feedback1",
      type: "feedbackTOP",
      position: [0, -100],
      inputs: ["level1"],
      color: [0.16, 0.72, 0.38]
    },
    {
      name: "transform1",
      type: "transformTOP",
      position: [300, -100],
      inputs: ["feedback1"]
    },
    {
      name: "composite1",
      type: "compositeTOP",
      position: [600, 100],
      inputs: ["noise1", "transform1"]
    },
    {
      name: "level1",
      type: "levelTOP",
      position: [900, 100],
      inputs: ["composite1"]
    },
    {
      name: "out",
      type: "nullTOP",
      position: [1200, 100],
      inputs: ["level1"]
    }
  ],
  annotations: [
    {
      title: "Feedback tunnel",
      text: "Noise is composited with a scaled feedback pass, then normalized for output.",
      position: [-90, -210],
      size: [1390, 560],
      color: [0.45, 0.45, 0.45]
    }
  ]
} satisfies Record<string, unknown>;
