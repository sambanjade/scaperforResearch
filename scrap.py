import requests
import pandas as pd
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

# Initialize data list
data = []

# Base URL and session setup with retries
base_url = "https://memberbase.com/ncbelsDiscDoc/showimage.aspx?Doc_Id="
session = requests.Session()
retry = Retry(connect=3, backoff_factor=0.5)
adapter = HTTPAdapter(max_retries=retry)
session.mount('http://', adapter)
session.mount('https://', adapter)
session.headers.update({'User-Agent': 'Mozilla/5.0'})

# Start and end range for Doc IDs
start_id = 185
end_id = 200  # Adjust as necessary

for doc_id in range(start_id, end_id + 1):
    url = f"{base_url}{doc_id}"
    print(f"Requesting URL for Doc_Id {doc_id}")

    try:
        response = session.get(url, allow_redirects=True)
        
        # Print headers for troubleshooting
        print(f"Headers for Doc_Id {doc_id}: {response.headers}")
        
        if response.status_code == 200:
            content = response.content
            if not content:
                print(f"No content for Doc_Id {doc_id}. Moving to next ID.")
                continue

            first_bytes = content[:4]
            print(f"First bytes of Doc_Id {doc_id}: {first_bytes}")

            # Ensure it's a valid PDF file
            if first_bytes == b'%PDF':
                print(f"Valid PDF detected for Doc_Id {doc_id}")
                
                # Append details to data list
                data.append({
                    "Case": f"Case_{doc_id}",
                    "Party": "Party Name Here",  # Placeholder for now
                    "Settle Date": "Settle Date Here",  # Placeholder for now
                    "Violation": "Violation Details Here",  # Placeholder for now
                    "Penalty": "Penalty Details Here"  # Placeholder for now
                })
            else:
                print(f"No valid PDF content for Doc_Id {doc_id}. Moving to next ID.")
        else:
            print(f"Request failed for Doc_Id {doc_id} with status code {response.status_code}.")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Doc_Id {doc_id}: {e}")
        continue

# Save data to an Excel file
if data:
    df = pd.DataFrame(data)
    df.to_excel("Case_Details.xlsx", index=False)
    print("Data saved to Case_Details.xlsx")
else:
    print("No valid PDF data to save.")
