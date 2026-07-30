### File 3: example_nodes.py (Sample Node Classes)

"""
Example node classes for testing the DAG system.
"""

class ConfigLoader:
    def run(self):
        """Load configuration."""
        print("🔧 Loading configuration...")
        return {"database": "production_db", "debug": False}


class DatabaseConnector:
    def run(self, config):
        """Connect to database using config."""
        print(f"🔌 Connecting to {config['database']}...")
        return "DB_CONNECTION_OBJECT"


class DataFetcher:
    def run(self, db_connection):
        """Fetch data from database."""
        print("📥 Fetching data...")
        return [
            {"id": 1, "amount": 150.0},
            {"id": 2, "amount": 300.0},
            {"id": 3, "amount": 75.5}
        ]


class DataProcessor:
    def run(self, data):
        """Process the fetched data."""
        print("⚙️ Processing data...")
        total = sum(item["amount"] for item in data)
        return {"total_amount": total, "record_count": len(data)}


class ReportGenerator:
    def run(self, processed_data):
        """Generate final report."""
        print("📊 Generating report...")
        return f"REPORT: ${processed_data['total_amount']:.2f} across {processed_data['record_count']} records"


def get_example_dag():
    """Return classes and dag for easy testing."""
    classes = {
        "ConfigLoader": ConfigLoader,
        "DatabaseConnector": DatabaseConnector,
        "DataFetcher": DataFetcher,
        "DataProcessor": DataProcessor,
        "ReportGenerator": ReportGenerator,
    }

    dag = {
        "ConfigLoader": [],
        "DatabaseConnector": ["ConfigLoader"],
        "DataFetcher": ["DatabaseConnector"],
        "DataProcessor": ["DataFetcher"],
        "ReportGenerator": ["DataProcessor"]
    }

    return classes, dag

### How to Test Everything

### Save all three files in the same folder.
### Run: python dag_code_generator.py
### It will generate xyz.py
### Run: python xyz.py

### You should see a clean execution of the full pipeline.
