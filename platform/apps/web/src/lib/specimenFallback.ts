import type { SpecimenSummary } from "@embody/contracts";
import fixtureSpecimens from "../fixtures/specimens.json";

export type FixtureSpecimen = (typeof fixtureSpecimens)[number];

export const fixtureSummaries: SpecimenSummary[] = fixtureSpecimens.map((specimen) => {
  return toFixtureSummary(specimen);
});

export function firstFixtureSpecimen(): FixtureSpecimen {
  const first = fixtureSpecimens[0];
  if (!first) {
    throw new Error("Missing specimen fixtures.");
  }
  return first;
}

export function findFixtureSpecimen(slug: string): FixtureSpecimen | undefined {
  return fixtureSpecimens.find((specimen) => specimen.slug === slug);
}

export function searchFixtureSummaries(query: string): SpecimenSummary[] {
  const terms = query
    .toLowerCase()
    .match(/[a-z0-9_]+/g);

  if (!terms?.length) return [];

  return fixtureSpecimens
    .filter((specimen) => {
      const haystack = [
        specimen.slug,
        specimen.name,
        specimen.category,
        specimen.description,
        specimen.want,
        specimen.prompt,
        specimen.evolve,
        ...specimen.tags,
        ...specimen.key_ops
      ]
        .join(" ")
        .toLowerCase();

      return terms.every((term) => haystack.includes(term));
    })
    .map((specimen) => toFixtureSummary(specimen));
}

export function toFixtureSummary(specimen: FixtureSpecimen): SpecimenSummary {
  return {
    slug: specimen.slug,
    name: specimen.name,
    category: specimen.category,
    difficulty: toDifficulty(specimen.difficulty),
    description: specimen.description,
    requires: specimen.requires,
    op_count: specimen.operator_count,
    thumbnail_key: "",
    author_handle: "embody.tools",
    tier: "featured",
    likes_count: 0,
    views_count: 0
  };
}

function toDifficulty(value: string): SpecimenSummary["difficulty"] {
  if (value === "starter" || value === "advanced") return value;
  return "intermediate";
}
