---- MODULE LocalGeneration ----
EXTENDS LocalAdmission

VARIABLES closed, requested, known, sessionId, outputId, generation, delivered
vars == <<closed, requested, known, sessionId, outputId, generation, delivered>>

Init == /\ closed = FALSE
        /\ requested = FALSE
        /\ known = FALSE
        /\ sessionId = 0
        /\ outputId = 0
        /\ generation = 1
        /\ delivered = FALSE

Frame == \E q, k \in BOOLEAN, s, o \in {0, 1, 2}:
    /\ requested' = q
    /\ known' = k
    /\ sessionId' = s
    /\ outputId' = o
    /\ delivered' = Accept(closed, q, k, s, o, generation)
    /\ UNCHANGED <<closed, generation>>

Close == /\ ~closed
         /\ closed' = TRUE
         /\ delivered' = FALSE
         /\ UNCHANGED <<requested, known, sessionId, outputId, generation>>

Replace == /\ closed
           /\ generation = 1
           /\ generation' = 2
           /\ closed' = FALSE
           /\ delivered' = FALSE
           /\ UNCHANGED <<requested, known, sessionId, outputId>>

Spec == Init /\ [][Frame \/ Close \/ Replace]_vars

OnlyCurrent == delivered =>
    /\ ~closed
    /\ ~requested \/ known
    /\ sessionId \in {0, generation}
    /\ outputId \in {0, generation}

CurrentAdmitted == ~closed /\ (~requested \/ known)
    /\ sessionId \in {0, generation} /\ outputId \in {0, generation}
    => Accept(closed, requested, known, sessionId, outputId, generation)
====
