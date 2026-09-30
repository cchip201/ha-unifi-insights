import {
    PROTECT_CARD_TAG,
    PROTECT_EDITOR_TAG,
    UnifiInsightsProtectStatusCard,
    UnifiInsightsProtectStatusCardEditor,
} from "./dashboard-cards";
import { registerDashboardCard } from "./register-dashboard-card";

registerDashboardCard({
    tag: PROTECT_CARD_TAG,
    editorTag: PROTECT_EDITOR_TAG,
    card: UnifiInsightsProtectStatusCard,
    editor: UnifiInsightsProtectStatusCardEditor,
    name: "UniFi Protect Status",
    description: "Live UniFi Protect camera, doorbell, chime, and NVR status summary.",
});

export {
    PROTECT_CARD_TAG,
    PROTECT_EDITOR_TAG,
    UnifiInsightsProtectStatusCard,
    UnifiInsightsProtectStatusCardEditor,
};
