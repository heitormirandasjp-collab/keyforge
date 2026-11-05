import assert from "node:assert/strict";
import test from "node:test";

import {
  modelStatus,
  overallSummary,
  providerSummary,
  summaryLabel,
  visibleModels,
} from "../src/models.js";

const GOOD = ["good/alpha", "good/beta", "good/gamma"];
const DEAD = ["dead/one", "dead/two"];

function configWith(statuses, extra = {}) {
  return {
    providers: {
      nvidia: {
        label: "NVIDIA NIM",
        models: [...GOOD, ...DEAD],
        defaultModel: GOOD[0],
        ...extra,
      },
    },
    modelHealth: statuses,
  };
}

const health = Object.fromEntries([
  ...GOOD.map((model) => [
    `nvidia::${model}`,
    { status: "ok", reason: "", checkedAt: "2026-11-04 17:00:00" },
  ]),
  ...DEAD.map((model) => [
    `nvidia::${model}`,
    { status: "error", reason: "404 · Function not found for account", checkedAt: "2026-11-04 17:00:00" },
  ]),
]);

test("dropdown mostra apenas os 3 modelos com status ok", () => {
  const config = configWith(health);
  const visible = visibleModels(config, "nvidia");
  assert.equal(visible.length, 3);
  assert.deepEqual(visible, GOOD);
});

test("toggle 'Mostrar modelos com erro' libera os 5, com ❌ nos reprovados", () => {
  const config = configWith(health);
  const visible = visibleModels(config, "nvidia", true);
  assert.equal(visible.length, 5);
  assert.equal(modelStatus(config, "nvidia", DEAD[0]), "error");
  assert.equal(modelStatus(config, "nvidia", GOOD[0]), "ok");
});

test("resumo no topo do seletor", () => {
  const config = configWith(health);
  assert.deepEqual(providerSummary(config, "nvidia"), {
    total: 5,
    ok: 3,
    error: 2,
    unknown: 0,
    tested: 5,
  });
  assert.equal(summaryLabel(overallSummary(config)), "3 de 5 modelos funcionando");
});

test("antes de qualquer teste o catálogo inteiro aparece", () => {
  const config = configWith({});
  assert.equal(visibleModels(config, "nvidia").length, 5);
  assert.equal(modelStatus(config, "nvidia", GOOD[0]), "unknown");
  assert.equal(overallSummary(config).tested, 0);
});

test("se nenhum modelo passou, o seletor não fica vazio", () => {
  const allFailed = Object.fromEntries(
    [...GOOD, ...DEAD].map((model) => [`nvidia::${model}`, { status: "error", reason: "404" }])
  );
  const config = configWith(allFailed);
  assert.equal(overallSummary(config).ok, 0);
  assert.equal(visibleModels(config, "nvidia").length, 5);
});

test("após um teste parcial, só o modelo aprovado aparece", () => {
  const partial = { [`nvidia::${GOOD[0]}`]: { status: "ok" } };
  const visible = visibleModels(configWith(partial), "nvidia");
  assert.deepEqual(visible, [GOOD[0]]);
  assert.equal(modelStatus(configWith(partial), "nvidia", GOOD[1]), "unknown");
});
