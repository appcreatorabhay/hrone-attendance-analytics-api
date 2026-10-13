\# Employee Attendance \& Analytics API



A RESTful Employee Attendance and Analytics API built with \*\*FastAPI\*\* and \*\*MongoDB\*\* for the HROne Software Engineer Hiring Assignment (Contract v2.0).



\## Features



\- Employee and attendance management APIs

\- Attendance analytics and reporting

\- Late arrival, overtime, and work-hours calculations

\- Pagination and filtering

\- MongoDB indexes for query performance

\- Health-check endpoint

\- Query execution-plan inspection for supported endpoints

\- Interactive API documentation using Swagger UI



\## Technology Stack



\- \*\*Language:\*\* Python 3.12.0

\- \*\*API Framework:\*\* FastAPI

\- \*\*Database:\*\* MongoDB Atlas Free Tier (M0)

\- \*\*Database Driver:\*\* PyMongo

\- \*\*API Server:\*\* Uvicorn



\## Prerequisites



Before running the application, install or configure:



1\. Python 3.12.0

2\. MongoDB Atlas account or a local MongoDB instance

3\. Git (if cloning the repository)

4\. A terminal such as Windows Command Prompt or PowerShell



Verify your Python installation:



```bash

python --version

```



Expected version:



```text

Python 3.12.0

```



\## MongoDB Atlas Setup



This project can use MongoDB Atlas Free Tier (M0).



1\. Create a cluster in MongoDB Atlas.

2\. Create a database user with the required database permissions.

3\. Configure Network Access to allow connections from your current IP address.

4\. Copy your cluster connection string from Atlas.

5\. Replace the username, password, and cluster placeholders in the connection string with your own values.



Keep your database credentials private. Never commit your real connection string or password to GitHub.



\## Installation on Windows



\### 1. Clone the repository



Replace the URL below with your public GitHub repository URL.



```bash

git clone https://github.com/YOUR\_USERNAME/hrone-attendance-analytics-api.git

cd hrone-attendance-analytics-api

```



Alternatively, download the repository as a ZIP file and extract it.



\### 2. Create a virtual environment



```bash

python -m venv .venv

```



\### 3. Activate the virtual environment



For Windows Command Prompt:



```cmd

.venv\\Scripts\\activate

```



For PowerShell:



```powershell

.\\.venv\\Scripts\\Activate.ps1

```



\### 4. Install dependencies



```bash

python -m pip install --upgrade pip

python -m pip install -r requirements.txt

```



\## Environment Configuration



Create a local file named `.env` in the project root directory, alongside `requirements.txt`.



Add the following configuration using your own MongoDB Atlas connection details:



```env

MONGO\_URI=mongodb+srv://<username>:<password>@<cluster-host>/?retryWrites=true\&w=majority

MONGO\_DB=attendance\_db

```



Replace the placeholders with your actual MongoDB Atlas credentials and cluster hostname. URL-encode special characters in the username or password when required.



\*\*Security:\*\* Keep `.env` local and do not upload it to GitHub. The application must receive `MONGO\_URI` and `MONGO\_DB` through its environment configuration.



\## Run the Application



With the virtual environment activated and `.env` configured, run:



```bash

python -m uvicorn app.main:app --reload --port 8000

```



The API will be available at:



\- \*\*Base URL:\*\* http://127.0.0.1:8000

\- \*\*Swagger UI:\*\* http://127.0.0.1:8000/docs

\- \*\*OpenAPI JSON:\*\* http://127.0.0.1:8000/openapi.json

\- \*\*Health Check:\*\* http://127.0.0.1:8000/health



Use Swagger UI to explore the available endpoints, provide request parameters, submit sample requests, and inspect responses.



The application creates its required MongoDB indexes during startup. The health endpoint returns HTTP 200 only when MongoDB responds successfully to a ping.



\## Sample Data (Optional)



The repository includes `sample\_seed.py` and the `sample\_data/` directory for loading the supplied sample documents.



Ensure the sample JSON files are present in the expected directory structure before running the seed script.



Run the seed script from the project root:



```bash

python sample\_seed.py

```



\*\*Warning:\*\* The seed script clears the relevant collections before inserting sample data. Do not run it against a database containing data you want to preserve.



\## Data and API Conventions



\- API instants use integer Unix epoch timestamps in milliseconds.

\- MongoDB stores instants as UTC BSON datetime values.

\- Calendar dates use the `YYYY-MM-DD` format.

\- Shift times use the `HH:MM` format in Indian Standard Time (IST).

\- Derived attendance values are calculated and stored by the API according to the assignment's business rules.

\- Internal MongoDB document identifiers are not exposed as public API identifiers.



Refer to `openapi.yaml` and `DATA\_MODEL.md` for the API contract and data model.



\## Project Structure



```text

hrone-attendance-analytics-api/

├── app/

│   └── main.py

├── sample\_data/

├── requirements.txt

├── sample\_seed.py

├── openapi.yaml

├── DATA\_MODEL.md

├── README.md

├── REVIEW.md

└── DECISIONS.md

```



A local `.env` file and `.venv/` directory are not included in the public repository.



\## Troubleshooting



\- \*\*MongoDB connection error:\*\* Verify the Atlas connection string, database user credentials, and Network Access IP allowlist.

\- \*\*Missing environment variables:\*\* Confirm that `.env` exists in the project root and contains valid `MONGO\_URI` and `MONGO\_DB` values.

\- \*\*Missing Python packages:\*\* Activate the virtual environment and reinstall dependencies from `requirements.txt`.

\- \*\*Port already in use:\*\* Run Uvicorn on another available port, for example `--port 8001`.

\- \*\*Timezone data error on Windows:\*\* Install the `tzdata` package if required by the installed Python environment.



\## Assignment Notes



This implementation follows the HROne Employee Attendance \& Analytics API assignment contract, version 2.0.



See `REVIEW.md` for the review notes and `DECISIONS.md` for implementation decisions.



