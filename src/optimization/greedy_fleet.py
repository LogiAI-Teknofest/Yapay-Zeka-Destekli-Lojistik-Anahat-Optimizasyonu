def assign_rentals_greedy(demands, rental_capacities):
    """
    1. AŞAMA: Talepleri büyükten küçüğe sıralar ve kiralık filoya dağıtır.
    Sığmayan (arta kalan) talepleri spot araçlar için listeler.
    """
    # Talepleri (Düğüm No, Yük Miktarı) olarak eşleştirip büyükten küçüğe diziyoruz.
    # 0. düğüm depo (toplama merkezi) olduğu için onu ayıklıyoruz.
    sorted_demands = sorted(
        [(node_id, demand) for node_id, demand in enumerate(demands) if node_id != 0 and demand > 0],
        key=lambda x: x[1], 
        reverse=True
    )
    
    rental_assignments = []
    
    # Elimizdeki her bir kiralık aracın kapasitesini tek tek doldurmaya başlıyoruz
    for i, capacity in enumerate(rental_capacities):
        current_capacity = capacity
        assigned_nodes = []
        remaining_demands = []
        
        # Büyük kargolardan başlayarak araca sığıp sığmadığına bakıyoruz
        for node_id, demand in sorted_demands:
            if demand <= current_capacity:
                assigned_nodes.append(node_id) # Kargoyu araca yükledik
                current_capacity -= demand     # Aracın kalan kapasitesini azalttık
            else:
                # Bu kargo mevcut araca sığmadı, bir sonraki aşama için kenara ayırıyoruz
                remaining_demands.append((node_id, demand))
                
        # Bu kiralık aracın planını kaydediyoruz
        rental_assignments.append({
            "vehicle_id": f"Rental_{i+1}",
            "assigned_nodes": assigned_nodes,
            "utilized_capacity": capacity - current_capacity,
            "total_capacity": capacity
        })
        
        # Bir sonraki kiralık araca sadece sığmayan kargoları devrediyoruz
        sorted_demands = remaining_demands
        
    # Kiralık araçlar tamamen dolduktan sonra yerde kalan yükleri,
    # OR-Tools'un anlayacağı eski [0, 45, 0, 0, 10] gibi liste formatına geri çeviriyoruz.
    spot_demands = [0] * len(demands)
    for node_id, demand in sorted_demands:
        spot_demands[node_id] = demand
        
    return rental_assignments, spot_demands