Data Pipeline Curation for Provider Rates
1. Business Context
US health plans must publish their negotiated rates with every in-network provider as Price Transparency Data (PTD) under the Transparency in Coverage rule. Payer contracting teams use competitors' PTD to see what the market pays for a service, by provider and by location.
Getting there is a data curation problem, not a display problem:
Finding the right network. A payer publishes index files covering hundreds of plans across brands and markets. Plan names rarely say which network they belong to.
Trusting the files. Files have $0 rates, duplicates, stale contracts and missing codes, and there is no standard for grading them.
Knowing who and where. Rates are tied to an NPI and a Tax ID (TIN), with no specialty and no address. One TIN can have many addresses and one NPI can practice at many addresses. About 80% of NPPES addresses are stale or wrong.
2. Problem Statement
Build a data pipeline that finds Centene's PPO network in its price transparency files, grades the quality of those files, and maps each provider in them to their correct practicing address, producing a provider-level rate file at the NPI, TIN, billing code and address level that a contracting team can use.
The hackathon focuses on three core problems: (1) Network Mapping, (2) Grading the quality of PTD files, and (3) PTD to Address mapping. All other steps support these three.
We care more about your logic than your tooling. Every decision must be explained.
3. Scope
Payer: Centene
Market: Commercial group, California
Network: PPO (you must identify which plans and files make it up)
Provider type: Type 1 (individual) and Type 2 (organization) NPIs
4. Data Provided
Dataset
Description
Source
Centene index files
All of Centene's index files (Ambetter, Health Net, QualChoice, Fidelis), as published
https://www.centene.com/price-transparency-files.html
NPPES extract
California NPIs with NPI type, taxonomy codes, and primary and secondary practice addresses. About 80% of addresses are stale
Refer: NPPES full download hyperlink on the URL, (Use VPN if need be) https://npiregistry.cms.hhs.gov/search
NUCC taxonomy
Maps taxonomy codes to specialties
Public
Specialty to Billing Code Mapping
Supplemented with the Case P.S. as an excel
[HiLabs Supplemented](https://hiringdrive2026.blob.core.windows.net/hackathon/Rate%20Benchmarking%20Individual%20Weights_Hackathon.xlsx)
You may use or suggest other public sources, for example payer provider directory APIs, the CMS Doctors and Clinicians file, or address standardization services. Explain what each one adds.
5. Challenges
The challenges run in order, and each builds on the previous one. They are grouped under the three core problems, with supporting steps marked.
Core Problem 1: Network Mapping
#
Challenge
What to build
C1
Parse the index files
Parse Centene's index files into one table: brand, plan name, plan ID, market type and rate file URL. Many plans point to the same file, so remove duplicates.
C2
Find the PPO network
Identify the PPO plans and rate files that make up the PPO network. Plan names are inconsistent and many do not state a network type. Explain your logic and how you ruled out HMO, EPO and POS.
Core Problem 2: Grading PTD Files
#
Challenge
What to build
C3
Grade the files
Define metrics for the quality and completeness of a PTD file (e.g. code coverage, provider coverage, $0 and outlier rates, rate types, duplicates, staleness). Score every file you use and justify each metric.
Supporting Step
#
Challenge
What to build
C4
Extract rates
For the PPO network, extract rates for every provider. Where a provider has several rates for one code, state which one you keep and why.
Core Problem 3: PTD to Address Mapping
#
Challenge
What to build
C5
Map PTD TINs and NPIs to their practicing address
NPPES addresses are highly stale, so do not rely on them alone.
Supporting Step
#
Challenge
What to build
C6
Build the provider rate file
Produce one file at the NPI, TIN, billing code and address level.
Brownie Point (optional)
#
Challenge
What to build
C7
Rate cost effectiveness
Define metrics for judging whether a provider is cost effective compared with similar providers. Define "similar" and defend each metric.
Constraint: One TIN can have multiple addresses, and one provider can work at multiple addresses. The relationship from TIN to NPI to address matters. Find a way to link them, show your logic, and use or suggest additional data sources.
Using HiLabs products: You may use existing HiLabs products (e.g. MCheck, R3) as building blocks. Known approaches, such as running MCheck R3 on NPPES addresses, are a baseline, not a solution. We are looking for thinking beyond that.
6. Deliverables
#
Deliverable
Details
1
Rate file (Excel)
One row per NPI, TIN, address and billing code, with columns NPI, TIN, Address, Billing Code, Specialty and Rate
2
Logic file
Your logic for (a) identifying the PPO network and (b) mapping TIN to NPI to address. Include the sources used, the rules applied and how you handled conflicts.
3
File quality metrics
The metrics used to grade PTD files, how each is calculated, and why.
4
Summary file
High-level metrics from your run, e.g. index files parsed, PPO plans and files identified, file quality scores, rates extracted, unique NPIs and TINs, and the share of NPIs linked to a verified address
5
Video
Max 5 minutes, explaining your solution
6
Code repo
Must re-run from the README, with no hardcoded paths
7
Brownie points
(a) Structured UI for executive and analyst views, (b) cost effectiveness metrics (C7)
7. Evaluation
Candidates are judged on the track they apply for.
Logic, metrics and structure Building -
Criterion
Points
What we look for
Logic
40
How the PPO network was identified, and how TIN, NPI, address and specialty were linked. Sound and well explained
Metrics
35
File quality metrics (C3) that are meaningful and defensible
Structure
25
A clear logic file, summary and video, and a problem broken down in a sensible order
Scalability of code and coverage of data linked
Criterion
Points
What we look for
Scalability
40
Streaming instead of loading whole files, parallel processing, and a design that would extend beyond the in-scope data (Section 3)
Coverage and linkage
35
Completeness on the in-scope data (Section 3), and share of NPIs and TINs linked to a verified address
Correctness and reproducibility
25
Extracted rates match the answer key on a sample, and the code re-runs cleanly
