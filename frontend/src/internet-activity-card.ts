import {
    INTERNET_ACTIVITY_CARD_TAG,
    INTERNET_ACTIVITY_EDITOR_TAG,
    UnifiInsightsInternetActivityCard,
    UnifiInsightsInternetActivityCardEditor,
} from "./dashboard-cards";
import { registerDashboardCard } from "./register-dashboard-card";

registerDashboardCard({
    tag: INTERNET_ACTIVITY_CARD_TAG,
    editorTag: INTERNET_ACTIVITY_EDITOR_TAG,
    card: UnifiInsightsInternetActivityCard,
    editor: UnifiInsightsInternetActivityCardEditor,
    name: "UniFi Internet Activity",
    description: "Historical WAN download/upload activity and live gateway throughput.",
});

export {
    INTERNET_ACTIVITY_CARD_TAG,
    INTERNET_ACTIVITY_EDITOR_TAG,
    UnifiInsightsInternetActivityCard,
    UnifiInsightsInternetActivityCardEditor,
};
