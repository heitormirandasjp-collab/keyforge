/**
 * O runtime 0.1.1 esconde tooltip/máscara em finish()/abort(), mas deixa o
 * <dialog> de bloqueio aberto. Fechar aqui evita que o app fique inerte após
 * concluir, cancelar ou abortar o tour. O próximo start() recria a camada.
 */
export function closeTourLayer() {
  document.querySelectorAll("dialog.wp-layer--blocking").forEach((node) => {
    if (!node.open) return;
    try {
      node.close();
    } catch {
      /* diálogo já removido */
    }
  });
  document.querySelectorAll(".wp-layer--pass").forEach((node) => {
    try {
      node.hidePopover?.();
    } catch {
      /* popover já fechado */
    }
  });
}
