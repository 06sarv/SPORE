from flask import Flask, render_template, request, jsonify
import pandas as pd
import geopandas as gpd
import numpy as np
from pathlib import Path
from sklearn.preprocessing import StandardScaler
from sklearn.neighbors import NearestNeighbors
from sklearn.utils.validation import check_is_fitted
import dbfread  # For reading DBF files
import os
from dotenv import load_dotenv

# For Gemini API
import requests

app = Flask(__name__)

# Global variables for data and models
soil_data = None
bacteria_data = None
scaler = StandardScaler()
model = NearestNeighbors(n_neighbors=5, algorithm='ball_tree')
soil_features = {}

load_dotenv()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

def load_data():
    global soil_data, bacteria_data, soil_features
    try:
        soil_data_path = Path('data/soilDB_shapefiles_and_attributes')
        
        # Load the shapefile
        print("\nLoading shapefile...")
        soil_data = gpd.read_file(soil_data_path / 'sgdbe4_0_20m.shp')
        
        # Load soil attributes from DBF files
        print("\nLoading soil attributes...")
        try:
            # Read the attribute files
            smu_sgdbe = pd.DataFrame(list(dbfread.DBF(soil_data_path / 'smu_sgdbe.dbf')))
            smu_ptrdb = pd.DataFrame(list(dbfread.DBF(soil_data_path / 'smu_ptrdb.dbf')))
            
            # Join the attribute data with the shapefile
            soil_data = soil_data.merge(smu_sgdbe, on='SMU', how='left')
            soil_data = soil_data.merge(smu_ptrdb, on='SMU', how='left')
            
        except Exception as e:
            print(f"Error loading DBF files: {str(e)}")
            return False

        # Define the 5 key soil characteristics with their descriptions
        soil_features = {
            'TEXT': {
                'name': 'Soil Texture',
                'description': 'Relative proportion of sand, silt, and clay (0: Coarse/Sandy to 8: Fine/Clayey)',
                'range': (0, 8),
                'mean': 2.06
            },
            'OC_TOP_P': {
                'name': 'Organic Carbon Content',
                'description': 'Percentage of organic carbon in topsoil (higher values indicate more organic matter)',
                'range': (30, 100),
                'mean': 88.47
            },
            'AWC_TOP_P': {
                'name': 'Available Water Capacity',
                'description': 'Percentage of water that soil can hold for plant and microbial use',
                'range': (34, 100),
                'mean': 92.47
            },
            'CEC_TOP_P': {
                'name': 'Cation Exchange Capacity',
                'description': 'Soil\'s ability to hold and supply nutrients (higher values indicate better nutrient retention)',
                'range': (40, 100),
                'mean': 91.82
            },
            'PHYSCHIM': {
                'name': 'Soil pH',
                'description': 'Soil acidity/alkalinity (1: Very acidic to 5: Very alkaline)',
                'range': (1, 5),
                'mean': 3.11
            }
        }

        # Verify all features exist in the data
        for feature in soil_features.keys():
            if feature not in soil_data.columns:
                print(f"WARNING: Feature {feature} not found in soil data!")
                return False

        # Prepare soil characteristics for scaling
        soil_data_features = soil_data[list(soil_features.keys())].fillna(soil_data[list(soil_features.keys())].mean())
        
        # Print statistics about the soil features
        print("\nSelected soil characteristics:")
        for feature, info in soil_features.items():
            print(f"\n{info['name']} ({feature}):")
            print(f"Description: {info['description']}")
            print(f"Range: {info['range'][0]} to {info['range'][1]}")
            print(f"Mean: {info['mean']:.2f}")
        
        # Fit the scaler and model
        scaler.fit(soil_data_features)
        model.fit(scaler.transform(soil_data_features))
        
        # Load GBIF bacteria data
        try:
            # Always use tab delimiter for this dataset
            bacteria_data = pd.read_csv('data/raw_gbif_data.csv', delimiter='\t')
            print("Loaded bacteria data with tab delimiter.")
            print(bacteria_data.head())
            if 'countryCode' in bacteria_data.columns:
                bacteria_data = bacteria_data[bacteria_data['countryCode'] == 'PL']
            print(f"Loaded {len(bacteria_data)} bacteria records for Poland")
        except Exception as e:
            print(f"Error loading bacteria data: {str(e)}")
            return False

        # Print bacteria data info after loading
        if bacteria_data is not None:
            print(f"\nBacteria data loaded: {len(bacteria_data)} records.")
            print(f"Columns: {bacteria_data.columns.tolist()}")
            if 'decimalLatitude' in bacteria_data.columns:
                print(f"Sample latitudes: {bacteria_data['decimalLatitude'].dropna().head(5).tolist()}")
            if 'decimalLongitude' in bacteria_data.columns:
                print(f"Sample longitudes: {bacteria_data['decimalLongitude'].dropna().head(5).tolist()}")
            if 'scientificName' in bacteria_data.columns:
                print(f"Sample names: {bacteria_data['scientificName'].dropna().head(5).tolist()}")
            if bacteria_data['decimalLatitude'].isnull().all() or bacteria_data['decimalLongitude'].isnull().all():
                print("WARNING: All latitude or longitude values are missing in bacteria data!")
        else:
            print("WARNING: No bacteria data loaded!")

        print("\nData loading completed successfully!")
        return True

    except Exception as e:
        print(f"Error in load_data: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

# Load data when the application starts
print("\nStarting data loading...")
load_data()

@app.route('/')
def home():
    # Get the ranges and descriptions for each feature
    feature_info = {}
    if soil_data is not None and soil_features:
        for feature, info in soil_features.items():
            non_null = soil_data[feature].dropna()
            if len(non_null) > 0:
                feature_info[feature] = {
                    'name': info['name'],
                    'description': info['description'],
                    'min': float(non_null.min()),
                    'max': float(non_null.max()),
                    'mean': float(non_null.mean())
                }
    
    return render_template('index.html', features=soil_features, feature_info=feature_info)

@app.route('/predict', methods=['POST'])
def predict():
    try:
        if not soil_features:
            return jsonify({'error': 'No soil features available. Please check the server logs.'}), 500

        data = request.get_json()
        
        # Create input array from the available features
        input_values = []
        for feature in soil_features:
            if feature in data:
                try:
                    value = float(data[feature])
                    # Validate against known ranges
                    if feature in soil_data:
                        non_null = soil_data[feature].dropna()
                        if len(non_null) > 0:
                            min_val = non_null.min()
                            max_val = non_null.max()
                            if not (min_val <= value <= max_val):
                                return jsonify({
                                    'error': f'Value for {feature} must be between {min_val:.2f} and {max_val:.2f}'
                                }), 400
                    input_values.append(value)
                except ValueError:
                    return jsonify({'error': f'Invalid value for {feature}'}), 400
            else:
                # Use mean value for missing features
                input_values.append(float(soil_data[feature].mean()))

        input_features = np.array([input_values])
        
        try:
            check_is_fitted(scaler)
        except Exception as e:
            print(f"Scaler not fitted: {str(e)}")
            return jsonify({'error': 'Soil data processing error. Please check server logs.'}), 500

        scaled_input = scaler.transform(input_features)
        distances, indices = model.kneighbors(scaled_input)
        
        predictions = []
        found_valid = False
        for idx, distance in zip(indices[0], distances[0]):
            soil_chars = soil_data.iloc[idx]
            # Skip if any selected feature is NaN
            if any(pd.isna(soil_chars[feature]) for feature in soil_features):
                continue
            found_valid = True
            try:
                # Check if geometry is valid and not None
                if soil_chars.geometry is None or not hasattr(soil_chars.geometry, 'bounds'):
                    continue
                bounds = soil_chars.geometry.bounds
                if bounds is None or len(bounds) != 4:
                    continue
                # Check if bacteria_data is valid and has required columns
                if bacteria_data is None or 'decimalLatitude' not in bacteria_data.columns or 'decimalLongitude' not in bacteria_data.columns:
                    print("WARNING: Bacteria data missing required columns.")
                    continue
                region_bacteria = bacteria_data[
                    (bacteria_data['decimalLatitude'] >= bounds[1]) &
                    (bacteria_data['decimalLatitude'] <= bounds[3]) &
                    (bacteria_data['decimalLongitude'] >= bounds[0]) &
                    (bacteria_data['decimalLongitude'] <= bounds[2])
                ]
                print(f"Region {idx}: Found {len(region_bacteria)} bacteria in polygon bounds.")
                # If no bacteria found in region, find the closest bacteria to the polygon center
                if region_bacteria.empty:
                    # Get polygon center
                    if hasattr(soil_chars.geometry, 'centroid'):
                        center = soil_chars.geometry.centroid
                        # Compute distances to all bacteria points
                        bacteria_data['distance'] = ((bacteria_data['decimalLatitude'] - center.y) ** 2 + (bacteria_data['decimalLongitude'] - center.x) ** 2) ** 0.5
                        region_bacteria = bacteria_data.nsmallest(3, 'distance')
                        print(f"Region {idx}: Found {len(region_bacteria)} bacteria by nearest fallback.")
                if region_bacteria.empty:
                    print(f"Region {idx}: No bacteria found even by fallback.")
                    continue
                bacteria_names = region_bacteria['scientificName'].unique()
                for bacteria in bacteria_names[:3]:
                    similarity_score = 1 / (1 + distance)
                    char_explanation = []
                    for feature in soil_features:
                        if feature in soil_chars:
                            feature_name = soil_features[feature]['name'] if feature in soil_features else feature
                            char_explanation.append(f"{feature_name}={soil_chars[feature]:.1f}")
                    predictions.append({
                        'name': bacteria,
                        'probability': float(similarity_score),
                        'explanation': f'Found in similar soil conditions in Poland with: ' + ', '.join(char_explanation)
                    })
            except Exception as e:
                print(f"Error processing region {idx}: {str(e)}")
                continue
        # If no valid polygons, use mean values for explanation
        if not found_valid:
            mean_values = {feature: float(soil_data[feature].mean()) for feature in soil_features}
            char_explanation = []
            for feature in soil_features:
                feature_name = soil_features[feature]['name'] if feature in soil_features else feature
                char_explanation.append(f"{feature_name}={mean_values[feature]:.1f}")
            predictions.append({
                'name': 'No specific microbe found',
                'probability': 0.0,
                'explanation': 'No matching soil polygon had all features present. Using mean values for Poland: ' + ', '.join(char_explanation)
            })
        seen_names = set()
        unique_predictions = []
        for pred in sorted(predictions, key=lambda x: x['probability'], reverse=True):
            if pred['name'] not in seen_names:
                seen_names.add(pred['name'])
                unique_predictions.append(pred)
                if len(unique_predictions) >= 5:
                    break
        if not unique_predictions:
            return jsonify({'microbes': [], 'message': 'No microbes found for the given soil characteristics. Try adjusting the values.'})
        return jsonify({'microbes': unique_predictions})

    except Exception as e:
        print(f"Error in predict: {str(e)}")
        return jsonify({'error': str(e)}), 400

@app.route('/explain_microbe', methods=['POST'])
def explain_microbe():
    data = request.get_json()
    microbe_name = data.get('name', '')
    soil_conditions = data.get('soil_conditions', '')
    if not GEMINI_API_KEY:
        return jsonify({'explanation': 'Gemini API key not set. Please add it to your .env file.'})
    prompt = (
        f"Explain, in 2-3 sentences, how the microbe '{microbe_name}' could survive in the following soil conditions: {soil_conditions}. "
        "Focus on the relationship between the microbe's biology and the soil properties."
    )
    try:
        # Gemini API call
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro-latest:generateContent"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 256}
        }
        response = requests.post(f"{url}?key={GEMINI_API_KEY}", headers=headers, json=payload)
        if response.status_code == 200:
            result = response.json()
            explanation = result['candidates'][0]['content']['parts'][0]['text']
            return jsonify({'explanation': explanation})
        else:
            return jsonify({'explanation': f'Gemini API error: {response.text}'}), 500
    except Exception as e:
        return jsonify({'explanation': f'Error contacting Gemini API: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(debug=True) 