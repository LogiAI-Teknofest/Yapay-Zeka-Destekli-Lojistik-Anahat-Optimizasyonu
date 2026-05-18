from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

def create_mock_data_model(spot_demands):
    """Esnek kısıtları ve sistem parametrelerini içeren sahte veri modeli."""
    data = {}
    data['distance_matrix'] = [
        [0, 2450, 710, 1000, 1600, 500],   # 0 (Depo)
        [2450, 0, 1700, 1500, 800, 400],   # 1. TM
        [710, 1700, 0, 350, 900, 600],     # 2. TM
        [1000, 1500, 350, 0, 700, 300],    # 3. TM
        [1600, 800, 900, 700, 0, 200],     # 4. TM
        [500, 400, 600, 300, 200, 0],      # 5. TM
    ]
    data['demands'] = spot_demands
    data['vehicle_capacities'] = [1000, 500, 250, 100] # Tır, Kamyon, Hafif Kamyon, Kamyonet
    data['num_vehicles'] = 4
    data['can_truck_dock'] = [1, 0, 1, 0, 1, 1] 
    data['depot'] = 0
    
    # Esnek kısıt parametreleri
    data['tm_handling_capacities'] = [99999, 1000, 1000, 1000, 50, 1000]
    data['penalty_handling_multiplier'] = 5  
    data['sla_deadlines'] = [0, 5000, 5000, 5000, 500, 5000]
    data['penalty_sla_multiplier'] = 10  
    
    return data


def run_fallback_algorithm(data):
    """
    B PLANI (FALLBACK): OR-Tools süre sınırında çözüm bulamazsa devreye girer.
    Kalan yükleri en yakın mesafedeki uygun spot araçlara hızla atar.
    """
    print("[Fallback] 🚨 OR-Tools süre sınırı aşıldı veya çözüm bulamadı! Kurtarma algoritması çalışıyor...")
    
    output = {
        "status": "Fallback (B Planı Devrede)", 
        "base_distance_cost": 0, 
        "total_penalty_cost": 0,
        "total_maliyet_Z": 0, 
        "routes": []
    }
    
    vehicle_types = ["Tır", "Kamyon", "Hafif Kamyon", "Kamyonet"]
    depot = data['depot']
    
    # Üzerinde yük olan düğümleri tespit et
    active_nodes = [node_id for node_id, demand in enumerate(data['demands']) if node_id != depot and demand > 0]
    
    # Basitçe, her aktif düğüm için en uygun aracı bulup git-gel rotası çiziyoruz (Mesafe bazlı hızlı kurtarma)
    for i, node_id in enumerate(active_nodes):
        demand = data['demands'][node_id]
        
        # En küçük hangi araç bu yükü taşır? (Kamyonetten Tıra doğru kontrol)
        chosen_vehicle_id = 3 # Varsayılan Kamyonet
        for v_id in reversed(range(data['num_vehicles'])):
            if data['vehicle_capacities'][v_id] >= demand:
                chosen_vehicle_id = v_id
                
        # Tır yanaşma kısıtı kontrolü
        if chosen_vehicle_id == 0 and data['can_truck_dock'][node_id] == 0:
            chosen_vehicle_id = 1 # Tır giremiyorsa Kamyona düşür
            
        distance = data['distance_matrix'][depot][node_id] + data['distance_matrix'][node_id][depot]
        
        # SLA ve TM Cezalarını hesapla
        sla_penalty = max(0, (data['distance_matrix'][depot][node_id] - data['sla_deadlines'][node_id])) * data['penalty_sla_multiplier']
        tm_penalty = max(0, (demand - data['tm_handling_capacities'][node_id])) * data['penalty_handling_multiplier']
        
        route_details = {
            "vehicle_id": chosen_vehicle_id,
            "vehicle_type": vehicle_types[chosen_vehicle_id],
            "path": [depot, node_id, depot],
            "route_load": demand,
            "route_distance": distance,
            "sla_penalties": sla_penalty
        }
        
        output["routes"].append(route_details)
        output["base_distance_cost"] += distance
        output["total_penalty_cost"] += (sla_penalty + tm_penalty)
        
    output["total_maliyet_Z"] = output["base_distance_cost"] + output["total_penalty_cost"]
    return output


def solve_spot_vrp(spot_demands):
    data = create_mock_data_model(spot_demands)
    
    manager = pywrapcp.RoutingIndexManager(len(data['distance_matrix']),
                                           data['num_vehicles'], data['depot'])
    routing = pywrapcp.RoutingModel(manager)

    # 1. Mesafe Kaydı
    def distance_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return data['distance_matrix'][from_node][to_node]

    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    # 2. Araç Kapasite Kısıtı
    def demand_callback(from_index):
        from_node = manager.IndexToNode(from_index)
        return data['demands'][from_node]

    demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_callback_index, 0, data['vehicle_capacities'], True, 'Capacity')

    # 3. Tır Yanaşma Kısıtı
    truck_vehicle_id = 0
    for node_id in range(1, len(data['distance_matrix'])):
        if data['can_truck_dock'][node_id] == 0:
            index = manager.NodeToIndex(node_id)
            routing.VehicleVar(index).RemoveValue(truck_vehicle_id)

    # 4. Boş Düğümleri Pas Geçme
    for node_id, demand in enumerate(data['demands']):
        if node_id != data['depot'] and demand == 0:
            index = manager.NodeToIndex(node_id)
            routing.AddDisjunction([index], 0)

    # 5. Süre/SLA Takip Boyutu
    routing.AddDimension(transit_callback_index, 0, 10000, True, 'Time')
    time_dimension = routing.GetDimensionOrDie('Time')

    # Çözüm parametreleri
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)
    
    # === SÜRE YÖNETİMİ ===
    # Gerçek projede burası 9 dakika (540 saniye) olacak. 
    # Test etmek için burayı milisaniyelere (örneğin 0 saniyeye) zorlayıp fallback'i tetikleyebiliriz.
    search_parameters.time_limit.seconds = 9 * 60  # 9 Dakika katı sınır

    # Çözümü bulmaya çalış
    solution = routing.SolveWithParameters(search_parameters)

    # OR-Tools'ta başarı durumu kontrolü routing.status() == 1 (ROUTING_SUCCESS) ile yapılır.
    if solution and routing.status() == 1:
        return format_output(data, manager, routing, solution, time_dimension)
    else:
        # Bulamazsa veya test amacıyla süre sınırını 0 yaptığında anında burası tetiklenir!
        return run_fallback_algorithm(data)


def format_output(data, manager, routing, solution, time_dimension):
    output = {"status": "Başarılı (OR-Tools Optimum)", "base_distance_cost": 0, "total_penalty_cost": 0, "total_maliyet_Z": 0, "routes": []}
    vehicle_types = ["Tır", "Kamyon", "Hafif Kamyon", "Kamyonet"]
    node_total_handling = [0] * len(data['distance_matrix'])
    
    for vehicle_id in range(data['num_vehicles']):
        index = routing.Start(vehicle_id)
        route_details = {"vehicle_id": vehicle_id, "vehicle_type": vehicle_types[vehicle_id], "path": [], "route_load": 0, "route_distance": 0, "sla_penalties": 0}
        
        while not routing.IsEnd(index):
            node_index = manager.IndexToNode(index)
            route_details["path"].append(node_index)
            arrival_time = solution.Value(time_dimension.CumulVar(index))
            if arrival_time > data['sla_deadlines'][node_index]:
                gecum = arrival_time - data['sla_deadlines'][node_index]
                route_details["sla_penalties"] += gecum * data['penalty_sla_multiplier']
            
            node_total_handling[node_index] += data['demands'][node_index]
            route_details["route_load"] += data['demands'][node_index]
            previous_index = index
            index = solution.Value(routing.NextVar(index))
            route_details["route_distance"] += routing.GetArcCostForVehicle(previous_index, index, vehicle_id)
            
        route_details["path"].append(manager.IndexToNode(index))
        if route_details["route_load"] > 0:
             output["routes"].append(route_details)
             output["base_distance_cost"] += route_details["route_distance"]
             output["total_penalty_cost"] += route_details["sla_penalties"]
             
    tm_penalties = 0
    for node_id, total_load in enumerate(node_total_handling):
        if total_load > data['tm_handling_capacities'][node_id]:
            asim = total_load - data['tm_handling_capacities'][node_id]
            tm_penalties += asim * data['penalty_handling_multiplier']
            
    output["total_penalty_cost"] += tm_penalties
    output["total_maliyet_Z"] = output["base_distance_cost"] + output["total_penalty_cost"]
    return output