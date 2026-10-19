/**
 * Pure helpers for reconciling backend sessions with the frontend tab registry.
 */
function findExternalSessions(serverTerminals, knownIds) {
    const known = knownIds instanceof Set ? knownIds : new Set(knownIds || []);
    return (serverTerminals || []).filter((session) => (
        session &&
        session.id &&
        !known.has(session.id) &&
        session.is_alive &&
        session.group_name &&
        session.path
    ));
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { findExternalSessions };
}
