# Research Task for Traders Application

## Overview

This project investigates whether selecting stocks from the largest U.S. companies by market capitalization can produce a higher five-year return than the S&P 500 benchmark.

The main analysis first identifies the Top 100 U.S. companies by market capitalization for each year, calculates their five-year total returns, calculates the corresponding S&P 500/SPY returns, and then compares the results. Additional exploratory experiments repeat the comparison using only the Top 50 and Top 20 companies.

## Setup

This project uses Python with [uv](https://docs.astral.sh/uv/) for dependency management.

Clone or download the project, then open a terminal in the project directory.

Install the project dependencies with:

```bash
uv sync
```

The required Python packages are:

```toml
dependencies = [
    "dotenv>=0.9.9",
    "massive>=2.8.0",
    "requests>=2.34.2",
]
```

## Environment Configuration

A Massive API key is required to download the market and company data used by the analysis.

Create a `.env` file in the project root:

```env
MASSIVE_API_KEY=your_api_key_here
```

The API key is loaded from the environment at runtime and is not written directly into the Python source code.

The `.env` file must not be committed to version control. Add it to `.gitignore`:

```gitignore
.env
```

Do not place real API credentials in source files, notebooks, configuration committed to the repository, or repository history.

## Running the Main Analysis

Run the scripts from the project root in the following order.

### 1. Generate the yearly Top 100 company lists

```bash
uv run python get_top100.py
```

This script obtains the largest U.S. companies by market capitalization for each selection year and saves the resulting Top 100 lists for later analysis.

### 2. Calculate the S&P 500 benchmark returns

```bash
uv run python sp500_return.py
```

This script calculates the corresponding five-year SPY/S&P 500 returns used as the benchmark.

### 3. Calculate five-year returns for the Top 100 stocks

```bash
uv run python calculate_top100_5y_returns.py
```

This script calculates the five-year total return of the selected Top 100 stocks, including price changes and dividends and handling the relevant historical ticker/corporate-action information.

### 4. Run the benchmark comparison

```bash
uv run python benchmark_returns.py
```

This script compares the Top 100 stock returns with the corresponding SPY returns and produces the main benchmark statistics used in the report.

## Additional Experiments

Two additional exploratory analyses test whether using a more concentrated group of the largest companies changes the result.

Run the Top 50 experiment with:

```bash
uv run python experiment_2_top50.py
```

Run the Top 20 experiment with:

```bash
uv run python experiment_3_top20.py
```

These experiments use the same underlying return data but restrict the analysis to the largest 50 and 20 companies respectively.

## Data Source and Processing

Market data is downloaded programmatically from the Massive API. The analysis uses historical price data, ticker/company information, market capitalization data, dividends, stock splits, and ticker-event information where required.

The project does not rely on manually prepared third-party datasets. Data downloaded from the API is processed by the scripts in this repository to produce the analysis outputs.

## Project Files

The main analysis scripts are:

```text
get_top100.py
sp500_return.py
calculate_top100_5y_returns.py
benchmark_returns.py
experiment_2_top50.py
experiment_3_top20.py
```

`__init__.py` is included as part of the Python project structure.

## Assistance and Source Disclosure

### Academic papers consulted

None.

### External datasets used

No separately downloaded external datasets were used. Market and company data were obtained programmatically from the Massive API.

### Existing repositories or code consulted

None.

### Tutorials or articles used

None.

### AI tools used

ChatGPT and OpenAI Codex were used during this project.

### What the AI tools were used for

ChatGPT and Codex were used to assist with writing, reviewing, debugging, and improving Python code, and with drafting and editing project documentation and written report content.

All generated or suggested material was reviewed and used under the author's responsibility. The author is responsible for understanding and defending every part of the submitted work.

### Assistance received from another person

None. The assessment was completed individually.
