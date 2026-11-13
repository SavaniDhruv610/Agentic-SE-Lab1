# OpenCode session statistics

| session | prompts | model calls | tool calls | input | cached input | output | reasoning | cost (USD) | minutes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| session_01.json | 1 | 4 | 3 | 8,687 | 25,631 | 168 | 52 | 0.00124 | 0.6 |
| session_02.json | 1 | 26 | 28 | 24,450 | 388,068 | 6,863 | 1,207 | 0.01036 | 3.0 |
| TOTAL | 2 | 30 | 31 | 33,137 | 413,699 | 7,031 | 1,259 | 0.01160 | 3.6 |

input = new input tokens, cached input = input tokens served from the provider cache (cheaper), reasoning = hidden reasoning tokens (billed as output).

### session_01.json: Creating and running hello.py

- Model: openai.gpt-6-luna
- Cost: 0.00124 USD for 1 prompt(s) and 4 model calls (0.00031 USD per call)
- Tools: bash x2, apply_patch x1
- Tokens sent to the model per call: first 8,483, last 8,678 (x1.0), sum 34,318

Context size per model call (every call re-sends the whole conversation so far):

```
call   1  #######################################      8,483
call   2  #######################################      8,545
call   3  ########################################     8,612
call   4  ########################################     8,678
```

### session_02.json: Create standalone responsive team page with embedded photos and AI vision section

- Model: openai.gpt-6-luna
- Cost: 0.01036 USD for 1 prompt(s) and 26 model calls (0.00040 USD per call)
- Tools: read x10, bash x8, todowrite x4, glob x3, apply_patch x2, grep x1
- Tokens sent to the model per call: first 9,151, last 24,375 (x2.7), sum 412,518

Context size per model call (every call re-sends the whole conversation so far):

```
call   1  ###############                              9,151
call   2  ###############                              9,355
call   3  ################                             9,576
call   4  ################                             9,712
call   5  #################                           10,139
call   6  #################                           10,561
call   7  ##################                          10,984
call   8  ###################                         11,395
call   9  ###################                         11,824
call  10  ####################                        12,052
call  11  #####################                       12,565
call  12  #####################                       12,976
call  13  #########################                   15,323
call  14  #########################                   15,406
call  15  ###########################                 16,652
call  16  ############################                16,793
call  17  ############################                16,997
call  18  ############################                17,060
call  19  ####################################        21,810
call  20  ####################################        22,014
call  21  ####################################        22,106
call  22  #####################################       22,273
call  23  #######################################     23,486
call  24  #######################################     23,760
call  25  ########################################    24,173
call  26  ########################################    24,375
```
