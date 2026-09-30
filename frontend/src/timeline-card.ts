import {
    TIMELINE_CARD_TAG,
    TIMELINE_EDITOR_TAG,
    UnifiInsightsTimelineCard,
    UnifiInsightsTimelineCardEditor,
} from "./dashboard-cards";
import { registerDashboardCard } from "./register-dashboard-card";

registerDashboardCard({
    tag: TIMELINE_CARD_TAG,
    editorTag: TIMELINE_EDITOR_TAG,
    card: UnifiInsightsTimelineCard,
    editor: UnifiInsightsTimelineCardEditor,
    name: "UniFi Event Timeline",
    description: "Recent UniFi Protect security and device activity timeline.",
});

export {
    TIMELINE_CARD_TAG,
    TIMELINE_EDITOR_TAG,
    UnifiInsightsTimelineCard,
    UnifiInsightsTimelineCardEditor,
};
