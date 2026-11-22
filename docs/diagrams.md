# Milestone 2 diagrams (Mermaid source; GitHub renders them)

## Figure 1 – Prompt workflow

```mermaid
flowchart TD
  Q([User question]) --> IN[Input check<br/>blocks write requests]
  IN --> RET[Schema retrieval +<br/>few-shot selection]
  RET --> P1[[P1 Generate SQL<br/>Gemini · JSON]]
  P1 -->|clarify / refuse /<br/>out of scope| NA([Answer without SQL])
  P1 --> SC{Static checks<br/>read-only · tables ·<br/>EXPLAIN dry run}
  SC -->|pass| P2[[P2 Verify<br/>independent reviewer]]
  SC -->|fail| P3[[P3 Correct<br/>max 2 attempts]]
  P2 -->|high-severity issue| P3
  P3 --> SC
  P2 -->|correct| EX[Run read-only<br/>timeout · row limit]
  EX -->|database error| P3
  EX --> OUT([Table + chart +<br/>explanation + confidence])
  OUT -.->|on request| P4[[P4 Explain SQL]]
```

## Figure 2 – Task decomposition

```mermaid
flowchart TD
  G[Answer a business question<br/>from a database] --> T1[1 Understand the question<br/>intent · ambiguity · safety]
  G --> T2[2 Find the relevant tables<br/>schema retrieval]
  G --> T3[3 Write the SQL<br/>P1 + 4 examples]
  G --> T4[4 Check and fix it<br/>code checks · P2 · P3]
  G --> T5[5 Run it safely<br/>read-only · 10 s timeout]
  G --> T6[6 Explain the answer<br/>explanation · chart · P4]
  T1 --- C1[code: input check<br/>P1: intent field]
  T2 --- C2[code: retriever]
  T4 --- C4[code: validator<br/>LLM: reviewer]
```

## Figure 3 – Input–output flow

```mermaid
flowchart LR
  subgraph IN [Inputs]
    direction TB
    I1[Question in English<br/>or Hinglish]
    I2[Database schema +<br/>data dictionary]
    I3[Conversation history<br/>for follow-ups]
    I4[Reference date]
  end
  subgraph OUT [Outputs]
    direction TB
    O1[Read-only SQL query]
    O2[Result table]
    O3[Chart when useful]
    O4[Plain-English explanation<br/>+ assumptions]
    O5[Confidence + review verdict]
    O6[Or: clarifying question /<br/>refusal / out-of-scope note]
  end
  IN --> PIPE[QueryBridge pipeline<br/>P1 → checks → P2 → P3 → run] --> OUT
```

## Figure 4 – System architecture

```mermaid
flowchart LR
  U([Browser<br/>HTML · CSS · JS]) -->|HTTPS · JSON API| SV[FastAPI web server<br/>Render free web service]
  SV --> PL[Query pipeline]
  PL --> PM[Prompt manager<br/>P1–P4 templates]
  PL --> SR[Schema retriever +<br/>example selector]
  PL --> VA[SQL validator<br/>sqlglot]
  PL --> EX[Read-only executor]
  PL -->|google-genai SDK| GM[(Gemini API<br/>gemini-3.5-flash-lite)]
  EX --> DB[(Demo database<br/>SQLite · 8 tables)]
  SV --> HS[(Question history<br/>SQLite)]
```

## Figure 5 – Retrieval pipeline

```mermaid
flowchart LR
  Q([Question]) --> SC[Score every table<br/>names · synonyms ·<br/>columns · sample values]
  DD[(Schema +<br/>data dictionary)] --> SC
  SC --> M{≤ 12 tables?}
  M -->|yes, demo DB| F[Full schema +<br/>relevant-table hint]
  M -->|no| RT[Top tables + bridge<br/>and lookup tables]
  Q --> PT[Detect SQL patterns<br/>top-N · anti-join · dates]
  LIB[(Few-shot library<br/>bookstore database)] --> SEL[Pick 4 examples<br/>pattern overlap + diversity]
  PT --> SEL
  F --> P1[[P1 prompt]]
  RT --> P1
  SEL --> P1
```

## Figure 6 – Self-correction loop

```mermaid
flowchart TD
  G[[P1 writes SQL]] --> C{Code checks pass?}
  C -->|no| R[[P3 repair<br/>attempt n of 2]]
  C -->|yes| V{P2 reviewer:<br/>correct?}
  V -->|high-severity issue| R
  V -->|yes| E{Runs without<br/>error?}
  E -->|no| R
  E -->|yes| D([Show result])
  R -->|n ≤ 2| C
  R -->|budget used up| W([Show best attempt<br/>with a warning])
```
