import {
    SITE_HEALTH_CARD_TAG,
    SITE_HEALTH_EDITOR_TAG,
    UnifiInsightsSiteHealthCard,
    UnifiInsightsSiteHealthCardEditor,
} from "./dashboard-cards";
import { registerDashboardCard } from "./register-dashboard-card";

registerDashboardCard({
    tag: SITE_HEALTH_CARD_TAG,
    editorTag: SITE_HEALTH_EDITOR_TAG,
    card: UnifiInsightsSiteHealthCard,
    editor: UnifiInsightsSiteHealthCardEditor,
    name: "UniFi Site Health",
    description: "Compact site health summary with WAN, gateway, device, and client status.",
});

export {
    SITE_HEALTH_CARD_TAG,
    SITE_HEALTH_EDITOR_TAG,
    UnifiInsightsSiteHealthCard,
    UnifiInsightsSiteHealthCardEditor,
};
