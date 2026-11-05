import React from "react";
import { createRoot } from "react-dom/client";
import { WaypointProvider } from "@waypoint-tours/react";
import { registerDict } from "@waypoint-tours/runtime";
import "@waypoint-tours/runtime/styles.css";
import App from "./App";
import { keyforgeTour } from "./tours/keyforgeTour";
import { applyTheme, getInitialTheme } from "./theme";
import { closeTourLayer } from "./tourLayer";
import { initLiquidGlass } from "./liquidGlass";
import "./styles.css";
import "./theme.css";

applyTheme(getInitialTheme());
initLiquidGlass();

registerDict("pt-BR", {
  next: "Próximo",
  prev: "Voltar",
  close: "Fechar",
  done: "Concluir",
  demoToast: "Modo demonstração",
  stepAnnounce: (index, total) => `Passo ${index} de ${total}`,
});

const tours = { [keyforgeTour.id]: keyforgeTour };

function tourEvent(name, detail) {
  window.dispatchEvent(new CustomEvent(name, { detail }));
}

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <WaypointProvider
      tours={tours}
      locale="pt-BR"
      theme="none"
      onStep={({ stepId }) => tourEvent("keyforge:tour-step", { stepId })}
      onFinish={() => {
        closeTourLayer();
        tourEvent("keyforge:tour-end", {});
      }}
      onAbort={() => {
        closeTourLayer();
        tourEvent("keyforge:tour-end", {});
      }}
    >
      <App />
    </WaypointProvider>
  </React.StrictMode>
);
